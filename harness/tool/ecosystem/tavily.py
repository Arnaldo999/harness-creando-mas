"""Tool `tavily_search` — búsqueda web vía API de Tavily.

Útil cuando el operador del CRM pregunta cosas de contexto de mercado
inmobiliario que no están en la DB (precios de zonas, normativas,
proveedores, etc.).

Si la API key no está configurada, la tool igual existe pero responde
con `{"error": "tavily no configurado"}` para que el modelo lo sepa.
"""

from __future__ import annotations

import json
import logging

import httpx

from harness.api import ToolDef
from harness.tool.registry import Tool

log = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"


class TavilySearchTool(Tool):
    def __init__(self, api_key: str | None, *, max_results: int = 5, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._max_results = max_results
        self._timeout = timeout

    def definition(self) -> ToolDef:
        return ToolDef(
            name="tavily_search",
            description=(
                "Búsqueda web (Tavily) para obtener contexto externo: precios "
                "de mercado, normativas, info de barrios/zonas, competencia, etc. "
                "Devuelve hasta 5 resultados con título, URL y snippet."
            ),
            input_schema={
                "query": {
                    "type": "string",
                    "description": "Consulta de búsqueda en lenguaje natural.",
                }
            },
            required=["query"],
        )

    async def execute(self, raw_input: str) -> tuple[str, bool]:
        try:
            args = json.loads(raw_input) if raw_input else {}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"input JSON inválido: {e}"}), True

        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "query requerida"}), True

        if not self._api_key:
            return (
                json.dumps(
                    {
                        "error": "tavily no configurado (falta TAVILY_API_KEY); avisá al usuario que la búsqueda web no está disponible."
                    }
                ),
                True,
            )

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": self._max_results,
            "search_depth": "basic",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(TAVILY_ENDPOINT, json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            log.info("tavily_http_error", extra={"err": str(e)[:200]})
            return json.dumps({"error": f"tavily HTTP error: {e}"}), True
        except Exception as e:
            log.info("tavily_error", extra={"err": str(e)[:200]})
            return json.dumps({"error": f"tavily error: {e}"}), True

        results = []
        for item in (data.get("results") or [])[: self._max_results]:
            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": item.get("content") or item.get("snippet"),
                }
            )
        return (
            json.dumps({"query": query, "results": results}, ensure_ascii=False),
            False,
        )
