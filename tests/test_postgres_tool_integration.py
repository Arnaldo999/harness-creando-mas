"""Tests de integración de QueryPostgresTool contra un Postgres REAL.

Esta es la pieza que el Cap 14 del blueprint (14_verificacion_testing.md)
describía como TARGET: integración SIN mocks. En vez de fingir la base de
datos, levantamos un postgres:16 efímero con testcontainers (ver el fixture
`pg_container` en conftest.py), lo llenamos con un schema mínimo y datos de
prueba, y corremos la tool real contra esa DB.

Lo que esto cubre y que `test_postgres_tool.py` (solo string ops) NO podía:

1. La ejecución E2E real: que el SELECT validado efectivamente traiga filas
   de Postgres (lo que hoy era "validación manual post-deploy").
2. El descubrimiento de tablas con `tenant_slug` vía information_schema REAL
   (`_ensure_tenant_tables`) — no se puede testear sin un Postgres de verdad.
3. La regresión del bug del 2026-05-24: un JOIN real NO debe romperse. El
   auto-inject de tenant_slug quedó desactivado justamente porque generaba
   `WHERE tenant_slug = X` ambiguo en JOINs. Este test es el candado para
   que nadie lo re-active sin un parser que sepa de aliases.
4. Que una keyword destructiva (DROP) sea rechazada ANTES de tocar la DB —
   verificado contra la DB real (la tabla sigue ahí después del intento).

Si no hay Docker, el fixture `pg_container` hace skip automático. La suite
rápida (`pytest -m 'not integration'`) no se ve afectada.

Diseño del orden (14.3): primero los caminos que NO son felices, el feliz
al final.
"""

from __future__ import annotations

import json

import pytest

from harness.tool.ecosystem.postgres import PostgresConnInfo, QueryPostgresTool

pytestmark = pytest.mark.integration


# -------------------------------------------------------------------------
# Schema + seed mínimos. Reproducen la forma real del CRM inmobiliario
# (leads + propiedades con tenant_slug, más una tabla SIN tenant_slug para
# verificar el descubrimiento selectivo de information_schema).
# -------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE leads (
    id          SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    nombre      TEXT NOT NULL,
    score       TEXT,
    propiedad_id INTEGER,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE propiedades (
    id          SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    titulo      TEXT NOT NULL,
    precio      INTEGER
);

-- Tabla SIN tenant_slug: el descubrimiento de information_schema NO debe
-- listarla como tabla-con-tenant.
CREATE TABLE config_global (
    clave  TEXT PRIMARY KEY,
    valor  TEXT
);
"""

_SEED_SQL = """
INSERT INTO propiedades (tenant_slug, titulo, precio) VALUES
    ('demo', 'Depto 2 amb Palermo', 120000),
    ('demo', 'Casa Tigre',          250000),
    ('otro', 'Lote Pilar',           80000);

INSERT INTO leads (tenant_slug, nombre, score, propiedad_id) VALUES
    ('demo', 'Juan Perez',   'caliente', 1),
    ('demo', 'Ana Gomez',    'tibio',    2),
    ('demo', 'Luis Torres',  'frio',     NULL),
    ('otro', 'Otro Tenant',  'caliente', 3);

INSERT INTO config_global (clave, valor) VALUES ('version', '1.0');
"""


@pytest.fixture
async def seeded_tool(pg_conn_info: PostgresConnInfo):
    """QueryPostgresTool conectado al Postgres efímero, con schema + datos
    ya cargados. Crea su propio pool (no inyectamos uno mockeado: es
    integración real). Lo cierra al final.
    """
    import asyncpg

    # Cargamos schema + seed con una conexión directa (fuera de la tool).
    conn = await asyncpg.connect(
        host=pg_conn_info.host,
        port=pg_conn_info.port,
        user=pg_conn_info.user,
        password=pg_conn_info.password,
        database=pg_conn_info.database,
    )
    try:
        # Idempotente: si una corrida previa dejó tablas, las tiramos.
        await conn.execute(
            "DROP TABLE IF EXISTS leads, propiedades, config_global CASCADE"
        )
        await conn.execute(_SCHEMA_SQL)
        await conn.execute(_SEED_SQL)
    finally:
        await conn.close()

    tool = QueryPostgresTool(pg_conn_info, tenant_slug="demo")
    try:
        yield tool
    finally:
        await tool.close()


def _result(raw: tuple[str, bool]) -> dict:
    """Parsea el (json_string, is_error) que devuelve execute()."""
    payload, _is_error = raw
    return json.loads(payload)


# -------------------------------------------------------------------------
# Caminos que NO son felices (primero, según 14.3)
# -------------------------------------------------------------------------


async def test_drop_no_toca_la_db(seeded_tool: QueryPostgresTool) -> None:
    """Un DROP se rechaza en el validador ANTES de llegar a Postgres.
    Verificación REAL: la tabla sigue existiendo y con sus filas después.
    """
    res = await seeded_tool.execute(json.dumps({"sql": "DROP TABLE leads"}))
    payload, is_error = res
    assert is_error is True
    assert "no permitida" in payload  # mensaje del validador

    # La DB no fue tocada: leads sigue respondiendo con sus 4 filas.
    after = _result(
        await seeded_tool.execute(json.dumps({"sql": "SELECT id FROM leads"}))
    )
    assert after["count"] == 4


async def test_sql_invalido_devuelve_error_no_crashea(
    seeded_tool: QueryPostgresTool,
) -> None:
    """Un SELECT a una columna inexistente vuelve como {'error': ...},
    no como excepción. El modelo lo lee y reformula.
    """
    res = await seeded_tool.execute(
        json.dumps({"sql": "SELECT columna_que_no_existe FROM leads"})
    )
    payload, is_error = res
    assert is_error is True
    data = json.loads(payload)
    assert "error" in data
    assert "sql_executed" in data  # devuelve la query que intentó


async def test_join_real_no_se_rompe(seeded_tool: QueryPostgresTool) -> None:
    """REGRESIÓN del bug 2026-05-24: el auto-inject de tenant_slug rompía
    JOINs generando `WHERE tenant_slug = X` ambiguo. Quedó desactivado.
    Este test es el candado: un JOIN real entre dos tablas que AMBAS tienen
    tenant_slug debe ejecutar sin error de ambigüedad.
    """
    sql = (
        "SELECT l.nombre, p.titulo "
        "FROM leads l JOIN propiedades p ON p.id = l.propiedad_id "
        "WHERE l.score = 'caliente'"
    )
    data = _result(await seeded_tool.execute(json.dumps({"sql": sql})))
    assert "error" not in data, f"el JOIN no debería romperse: {data.get('error')}"
    # demo tiene 1 lead caliente con propiedad (Juan Perez → Palermo).
    # 'otro' tenant también tiene uno, pero sin filtro de tenant ambos vuelven;
    # lo que importa para la regresión es que NO crashee por ambigüedad.
    assert data["count"] >= 1
    assert any(r["nombre"] == "Juan Perez" for r in data["rows"])


# -------------------------------------------------------------------------
# Descubrimiento de tenant_slug vía information_schema REAL
# -------------------------------------------------------------------------


async def test_descubre_solo_tablas_con_tenant_slug(
    seeded_tool: QueryPostgresTool,
) -> None:
    """`_ensure_tenant_tables` consulta information_schema del Postgres real.
    Debe listar leads y propiedades (tienen tenant_slug) pero NO config_global.
    Esto solo se puede testear contra una DB real — es el corazón de por qué
    'sin mock'.
    """
    tablas = await seeded_tool._ensure_tenant_tables()
    assert "leads" in tablas
    assert "propiedades" in tablas
    assert "config_global" not in tablas


# -------------------------------------------------------------------------
# Camino feliz (al final, según 14.3) — verificación CONTRA la DB
# -------------------------------------------------------------------------


async def test_select_trae_filas_reales(seeded_tool: QueryPostgresTool) -> None:
    """El happy path: un SELECT válido trae las filas que sembramos."""
    data = _result(
        await seeded_tool.execute(
            json.dumps({"sql": "SELECT nombre, score FROM leads WHERE score = 'caliente'"})
        )
    )
    assert "error" not in data
    # demo + otro tienen 1 caliente cada uno = 2 (sin filtro de tenant activo).
    nombres = {r["nombre"] for r in data["rows"]}
    assert "Juan Perez" in nombres


async def test_limit_forzado_se_aplica_de_verdad(
    seeded_tool: QueryPostgresTool,
) -> None:
    """Sin LIMIT explícito, la tool fuerza LIMIT 100 — y eso se ve en la
    query ejecutada real contra Postgres.
    """
    data = _result(
        await seeded_tool.execute(json.dumps({"sql": "SELECT id FROM propiedades"}))
    )
    assert "error" not in data
    assert "LIMIT 100" in data["sql_executed"]
    assert data["count"] == 3  # las 3 propiedades sembradas


async def test_limit_explicito_se_respeta(seeded_tool: QueryPostgresTool) -> None:
    """Si el SELECT ya trae LIMIT, no se pisa — y limita de verdad las filas."""
    data = _result(
        await seeded_tool.execute(
            json.dumps({"sql": "SELECT id FROM leads ORDER BY id LIMIT 2"})
        )
    )
    assert "error" not in data
    assert data["count"] == 2
    assert data["sql_executed"].count("LIMIT") == 1
