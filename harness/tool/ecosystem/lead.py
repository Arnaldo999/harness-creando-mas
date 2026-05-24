"""Tools helper específicas de la tabla `leads`.

- `lookup_lead(telefono)` — wrapper de SELECT con tenant filter.
- `update_lead_estado(lead_id, nuevo_estado)` — bypass del validador SQL
  (esta sí es UPDATE), pero validando el enum de estados permitidos.

Ambas reutilizan el pool de QueryPostgresTool — el loader del tenant
inyecta la misma instancia de PostgresConnInfo + (opcionalmente) un
pool compartido.
"""

from __future__ import annotations

import json
import logging

import asyncpg

from harness.api import ToolDef
from harness.tool.ecosystem.postgres import PostgresConnInfo
from harness.tool.registry import Tool

log = logging.getLogger(__name__)

# Enum de estados válidos del lead (espejo del CRM).
ESTADOS_VALIDOS_LEAD = {
    "no_contactado",
    "contactado",
    "calificado",
    "visita_agendada",
    "visito",
    "en_negociacion",
    "seguimiento",
    "cerrado_ganado",
    "cerrado_perdido",
}


class LookupLeadTool(Tool):
    """SELECT * FROM leads WHERE telefono = $1 LIMIT 1, con tenant filter."""

    def __init__(
        self,
        conn_info: PostgresConnInfo,
        tenant_slug: str,
        *,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._conn_info = conn_info
        self._tenant_slug = tenant_slug
        self._pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="lookup_lead",
            description=(
                "Busca un lead por número de teléfono. Devuelve todos los "
                "campos del lead (id, nombre, score, tipo_propiedad, etc.) "
                "o null si no existe. El filtro de tenant se aplica automáticamente."
            ),
            input_schema={
                "telefono": {
                    "type": "string",
                    "description": "Número de teléfono del lead (formato libre — la búsqueda es exacta).",
                }
            },
            required=["telefono"],
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
                max_size=3,
            )
        return self._pool

    async def execute(self, raw_input: str) -> tuple[str, bool]:
        try:
            args = json.loads(raw_input) if raw_input else {}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"input JSON inválido: {e}"}), True

        telefono = args.get("telefono", "").strip()
        if not telefono:
            return json.dumps({"error": "telefono requerido"}), True

        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM leads WHERE telefono = $1 AND tenant_slug = $2 LIMIT 1",
                    telefono,
                    self._tenant_slug,
                )
        except Exception as e:
            log.info("lookup_lead_error", extra={"err": str(e)[:200]})
            return json.dumps({"error": f"DB error: {e}"}), True

        if row is None:
            return json.dumps({"lead": None}), False
        return json.dumps({"lead": dict(row)}, default=str, ensure_ascii=False), False


class UpdateLeadEstadoTool(Tool):
    """UPDATE controlado del campo `estado` de un lead.

    Bypassa el validador SQL — es la ÚNICA tool que ejecuta UPDATE en
    Fase 0. Valida el enum del nuevo estado y filtra por tenant_slug.
    Logea cada operación.
    """

    def __init__(
        self,
        conn_info: PostgresConnInfo,
        tenant_slug: str,
        *,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._conn_info = conn_info
        self._tenant_slug = tenant_slug
        self._pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="update_lead_estado",
            description=(
                "Actualiza el campo `estado` de un lead específico. "
                "Estados válidos: no_contactado, contactado, calificado, "
                "visita_agendada, visito, en_negociacion, seguimiento, "
                "cerrado_ganado, cerrado_perdido."
            ),
            input_schema={
                "lead_id": {
                    "type": "integer",
                    "description": "ID del lead a actualizar.",
                },
                "nuevo_estado": {
                    "type": "string",
                    "description": "Nuevo estado del lead (debe estar en el enum permitido).",
                },
            },
            required=["lead_id", "nuevo_estado"],
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
                max_size=3,
            )
        return self._pool

    async def execute(self, raw_input: str) -> tuple[str, bool]:
        try:
            args = json.loads(raw_input) if raw_input else {}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"input JSON inválido: {e}"}), True

        lead_id = args.get("lead_id")
        nuevo_estado = (args.get("nuevo_estado") or "").strip().lower()

        if not isinstance(lead_id, int):
            return json.dumps({"error": "lead_id debe ser entero"}), True
        if nuevo_estado not in ESTADOS_VALIDOS_LEAD:
            return (
                json.dumps(
                    {
                        "error": f"estado inválido: {nuevo_estado!r}",
                        "estados_validos": sorted(ESTADOS_VALIDOS_LEAD),
                    }
                ),
                True,
            )

        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                status = await conn.execute(
                    "UPDATE leads SET estado = $1 WHERE id = $2 AND tenant_slug = $3",
                    nuevo_estado,
                    lead_id,
                    self._tenant_slug,
                )
        except Exception as e:
            log.info("update_lead_estado_error", extra={"err": str(e)[:200]})
            return json.dumps({"error": f"DB error: {e}"}), True

        # asyncpg devuelve algo como "UPDATE 1" o "UPDATE 0".
        affected = 0
        parts = status.split()
        if len(parts) >= 2 and parts[0] == "UPDATE":
            try:
                affected = int(parts[1])
            except ValueError:
                affected = 0

        log.info(
            "update_lead_estado",
            extra={
                "tenant": self._tenant_slug,
                "lead_id": lead_id,
                "nuevo_estado": nuevo_estado,
                "affected": affected,
            },
        )

        if affected == 0:
            return (
                json.dumps(
                    {
                        "ok": False,
                        "affected": 0,
                        "msg": "lead no encontrado o pertenece a otro tenant",
                    }
                ),
                False,
            )

        return json.dumps({"ok": True, "affected": affected, "nuevo_estado": nuevo_estado}), False
