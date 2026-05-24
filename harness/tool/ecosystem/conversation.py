"""Tool `generar_resumen_conversacion` — lookup del resumen pre-existente.

Fase 0: SOLO lookup (sin generación). La tabla `resumenes_conversacion`
tiene índice UNIQUE en `(tenant_slug, telefono)`. Si existe el row,
devolvemos el `resumen`; si no, "Sin conversación previa registrada".

Fase 1 va a generar el resumen on-demand a partir del historial
WhatsApp si no existe. Hoy queda fuera de scope.
"""

from __future__ import annotations

import json
import logging

import asyncpg

from harness.api import ToolDef
from harness.tool.ecosystem.postgres import PostgresConnInfo
from harness.tool.registry import Tool

log = logging.getLogger(__name__)


class GenerarResumenConversacionTool(Tool):
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
            name="generar_resumen_conversacion",
            description=(
                "Devuelve el resumen guardado de la conversación de un lead, "
                "buscando por número de teléfono. Si el lead nunca tuvo "
                "conversación registrada, devuelve un mensaje indicándolo."
            ),
            input_schema={
                "telefono": {
                    "type": "string",
                    "description": "Número de teléfono del lead.",
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

        telefono = (args.get("telefono") or "").strip()
        if not telefono:
            return json.dumps({"error": "telefono requerido"}), True

        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT resumen, updated_at
                    FROM resumenes_conversacion
                    WHERE telefono = $1 AND tenant_slug = $2
                    LIMIT 1
                    """,
                    telefono,
                    self._tenant_slug,
                )
        except Exception as e:
            log.info("generar_resumen_error", extra={"err": str(e)[:200]})
            return json.dumps({"error": f"DB error: {e}"}), True

        if row is None:
            return (
                json.dumps(
                    {
                        "telefono": telefono,
                        "resumen": "Sin conversación previa registrada.",
                        "existe": False,
                    },
                    ensure_ascii=False,
                ),
                False,
            )

        return (
            json.dumps(
                {
                    "telefono": telefono,
                    "resumen": row["resumen"],
                    "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                    "existe": True,
                },
                ensure_ascii=False,
            ),
            False,
        )
