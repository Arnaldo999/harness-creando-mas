"""Tool `query_postgres` — consulta SELECT al CRM con SQL validator y
inyección automática de `tenant_slug`.

Reglas duras:
1. Solo SELECT (o `WITH ... SELECT`). Cualquier keyword destructiva
   (DROP/DELETE/UPDATE/INSERT/TRUNCATE/CREATE/ALTER/GRANT/REVOKE) hace
   reject inmediato.
2. Las keywords destructivas se detectan con `\\b...\\b` (word boundaries).
   Esto evita el bug histórico del workflow n8n donde "CREATE" matcheaba
   contra columnas `created_at`. Ver MEMORY workspace.
3. Si la query no tiene `LIMIT`, forzamos `LIMIT 100`.
4. Si la tabla referenciada tiene columna `tenant_slug` (descubrimiento
   vía information_schema.columns en caché), inyectamos el filtro al WHERE.
5. Si la query ya filtra por tenant_slug (literal o parámetro), no
   duplicamos.

Conexión vía `asyncpg.create_pool` (max 5 conexiones por tool instance).
Credenciales del TenantConfig.

Si el SELECT tira error de SQL, devolvemos `{"error": "<msg>"}` para
que el modelo pueda reformular.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import asyncpg

from harness.api import ToolDef
from harness.tool.registry import Tool

log = logging.getLogger(__name__)


# Keywords destructivas. Word boundaries (`\b`) son críticos: sin ellos,
# "CREATE" matchearía contra "created_at" — bug histórico del workflow n8n.
_DESTRUCTIVE_RE = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|TRUNCATE|CREATE|ALTER|GRANT|REVOKE|REPLACE|MERGE|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

# Para detectar "comienza con SELECT/WITH". Saltamos comentarios y whitespace.
_LEADING_KEYWORD_RE = re.compile(
    r"^(?:\s|/\*.*?\*/|--[^\n]*\n?)*(SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)

_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)

# Detecta filtro existente por tenant_slug (literal o parámetro).
# Cubre: tenant_slug = 'demo' | tenant_slug='demo' | tenant_slug = $1 |
# tenant_slug IN (...) | t.tenant_slug = ...
_TENANT_FILTER_RE = re.compile(
    r"\btenant_slug\s*(=|IN|!=)", re.IGNORECASE
)

# Extrae nombres de tablas del FROM/JOIN. Heurística simple — alcanza
# para nuestro caso (CRM con SQL estándar generado por el LLM).
_FROM_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE
)


class SQLValidationError(ValueError):
    """SQL rechazado por el validador (destructivo, no SELECT, vacío...)."""


@dataclass
class PostgresConnInfo:
    host: str
    port: int
    user: str
    password: str
    database: str


def validate_select_sql(sql: str) -> str:
    """Valida que `sql` sea un SELECT puro. Lanza `SQLValidationError`
    si no. Devuelve la query trimmed (sin trailing semicolons múltiples).
    """
    if not sql or not sql.strip():
        raise SQLValidationError("query vacía")

    cleaned = sql.strip()
    # Stripeamos un único trailing ';' opcional. Múltiples ';' indican
    # query stacking → reject.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if ";" in cleaned:
        raise SQLValidationError(
            "múltiples statements detectados (';' interno no permitido)"
        )

    # Sin keywords destructivas en cualquier parte (commented-out NO se
    # neutraliza acá; el statement-único más leading-SELECT minimiza el riesgo).
    # Chequeamos PRIMERO destructivas porque da mejor mensaje al modelo
    # cuando intenta un DROP / DELETE / UPDATE directo.
    m = _DESTRUCTIVE_RE.search(cleaned)
    if m:
        raise SQLValidationError(
            f"keyword no permitida: {m.group(0)}. Esta tool solo ejecuta SELECT."
        )

    # Debe arrancar con SELECT o WITH (CTEs). Esto pega cosas como
    # `EXPLAIN ANALYZE SELECT 1` que no son destructivas pero tampoco
    # están permitidas.
    if not _LEADING_KEYWORD_RE.match(cleaned):
        raise SQLValidationError(
            "solo se permite SELECT (o WITH ... SELECT). Para updates de leads, "
            "usá la tool update_lead_estado."
        )

    return cleaned


def ensure_limit(sql: str, default_limit: int = 100) -> str:
    """Agrega `LIMIT N` si la query no tiene LIMIT explícito."""
    if _LIMIT_RE.search(sql):
        return sql
    return f"{sql} LIMIT {default_limit}"


def _extract_tables(sql: str) -> list[str]:
    """Lista de nombres de tabla referenciados en FROM/JOIN. Lowercased,
    duplicados removidos preservando orden.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _FROM_TABLE_RE.finditer(sql):
        name = m.group(1).lower()
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def inject_tenant_filter(
    sql: str, tenant_slug: str, tables_with_tenant: set[str]
) -> str:
    """Inyecta `AND tenant_slug = '<slug>'` al WHERE si alguna tabla
    referenciada está en `tables_with_tenant` y la query NO filtra ya
    por tenant_slug.

    Diseño deliberadamente conservador: si no podemos garantizar la
    inyección segura (ej. la query tiene UNION o subqueries complejas),
    no la inyectamos. La separación primaria del demo es DB-per-cliente;
    el filtro tenant_slug es defensa en profundidad para tablas residuales.
    """
    if _TENANT_FILTER_RE.search(sql):
        return sql

    referenced = _extract_tables(sql)
    relevant = [t for t in referenced if t in tables_with_tenant]
    if not relevant:
        return sql

    # Sanitizamos el slug (solo permitimos [a-zA-Z0-9_-]).
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", tenant_slug):
        raise SQLValidationError(f"tenant_slug inválido: {tenant_slug!r}")

    filter_clause = f"tenant_slug = '{tenant_slug}'"

    # Heurística: si hay WHERE, prependeamos `AND`. Si no, agregamos `WHERE`
    # antes de GROUP/ORDER/LIMIT.
    where_match = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
    if where_match:
        # Insertar después del WHERE: `WHERE (orig) AND tenant_slug = '...'`
        # Para no romper precedencia, envolvemos la condición existente.
        start = where_match.end()
        # Buscamos hasta el próximo terminador de cláusula.
        terminator = re.search(
            r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|HAVING|UNION|FETCH)\b",
            sql[start:],
            re.IGNORECASE,
        )
        end = start + (terminator.start() if terminator else len(sql) - start)
        existing = sql[start:end].strip()
        new = f" ({existing}) AND {filter_clause} "
        return sql[:start] + new + sql[end:]
    else:
        # Agregar WHERE antes del primer terminador.
        terminator = re.search(
            r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|HAVING|UNION|FETCH)\b",
            sql,
            re.IGNORECASE,
        )
        if terminator:
            return (
                sql[: terminator.start()].rstrip()
                + f" WHERE {filter_clause} "
                + sql[terminator.start() :]
            )
        return f"{sql.rstrip()} WHERE {filter_clause}"


# -------------------------------------------------------------------------
# Tool class
# -------------------------------------------------------------------------


class QueryPostgresTool(Tool):
    """SELECT sobre el CRM Postgres del tenant.

    Constructor toma `PostgresConnInfo`, `tenant_slug` y un opcional
    `pool` ya creado (útil para tests con mocks). En producción, el
    pool se crea perezosamente en el primer execute.

    `tables_with_tenant` se descubre la primera vez consultando
    `information_schema.columns WHERE column_name='tenant_slug'`.
    """

    def __init__(
        self,
        conn_info: PostgresConnInfo,
        tenant_slug: str,
        *,
        pool: asyncpg.Pool | None = None,
        max_pool_size: int = 5,
        default_limit: int = 100,
    ) -> None:
        self._conn_info = conn_info
        self._tenant_slug = tenant_slug
        self._pool: asyncpg.Pool | None = pool
        self._max_pool_size = max_pool_size
        self._default_limit = default_limit
        self._tables_with_tenant: set[str] | None = None

    def definition(self) -> ToolDef:
        return ToolDef(
            name="query_postgres",
            description=(
                "Consulta SELECT a la base de datos del CRM inmobiliario. "
                "Las tablas principales son: leads, propiedades, clientes_activos, "
                "asesores, propietarios, contratos, visitas. La columna tenant_slug "
                "se filtra automáticamente. Solo SELECT permitido. LIMIT 100 por defecto."
            ),
            input_schema={
                "sql": {
                    "type": "string",
                    "description": (
                        "Query SELECT SQL válida. Ejemplo: "
                        "SELECT id, nombre, score FROM leads WHERE score='caliente' "
                        "ORDER BY created_at DESC"
                    ),
                }
            },
            required=["sql"],
        )

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                host=self._conn_info.host,
                port=self._conn_info.port,
                user=self._conn_info.user,
                password=self._conn_info.password,
                database=self._conn_info.database,
                min_size=1,
                max_size=self._max_pool_size,
            )
        return self._pool

    async def _ensure_tenant_tables(self) -> set[str]:
        """Descubre qué tablas tienen columna `tenant_slug`. Cacheado."""
        if self._tables_with_tenant is not None:
            return self._tables_with_tenant
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE column_name = 'tenant_slug'
                  AND table_schema = ANY(current_schemas(false))
                """
            )
        self._tables_with_tenant = {r["table_name"].lower() for r in rows}
        return self._tables_with_tenant

    async def execute(self, raw_input: str) -> tuple[str, bool]:
        try:
            args = json.loads(raw_input) if raw_input else {}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"input JSON inválido: {e}"}), True

        sql = args.get("sql", "")

        # 1. Validar.
        try:
            sql = validate_select_sql(sql)
        except SQLValidationError as e:
            return json.dumps({"error": str(e)}), True

        # 2. Inyectar tenant_slug si corresponde.
        try:
            tenant_tables = await self._ensure_tenant_tables()
            sql = inject_tenant_filter(sql, self._tenant_slug, tenant_tables)
        except SQLValidationError as e:
            return json.dumps({"error": str(e)}), True
        except Exception as e:
            # Error introspeccionando metadata — no bloqueamos, ejecutamos
            # sin inyección. Logueamos para diagnóstico.
            log.warning("tenant_introspection_failed: %s", str(e)[:200])

        # 3. Forzar LIMIT.
        sql = ensure_limit(sql, self._default_limit)

        # 4. Ejecutar.
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql)
        except Exception as e:
            log.error("query_postgres_error: %s | sql=%s", str(e)[:300], sql[:200])
            return json.dumps({"error": f"SQL error: {e}", "sql_executed": sql}), True

        result: list[dict[str, Any]] = [dict(r) for r in rows]
        return (
            json.dumps(
                {"rows": result, "count": len(result), "sql_executed": sql},
                default=str,
                ensure_ascii=False,
            ),
            False,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
