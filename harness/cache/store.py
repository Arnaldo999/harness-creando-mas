"""ResponseCache — TTL in-memory por (tenant, mensaje normalizado).

Razones de diseño:
- Las consultas del CRM de Lovbot son repetitivas en ventanas cortas
  ("¿cuántos leads calientes tengo?", "¿qué vendí hoy?"). Cachear
  evita pegarle a OpenAI con costo + latencia para idempotentes.
- TTL corto (30s default) porque el estado del CRM cambia constantemente
  — no queremos servir leads stale.
- Skip si la conversación tocó tools de escritura: si el modelo cambió
  estado, no podemos asumir que la próxima query traiga lo mismo.

Implementación: `cachetools.TTLCache` + `asyncio.Lock` para thread-safety
dentro del event loop.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache

# Tools que mutan estado en backend → respuestas que dependen de ellas
# NO se cachean. Agregar acá las nuevas write-tools del ecosistema.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "update_lead_estado",
        # Futuras:
        # "create_lead",
        # "send_whatsapp_message",
    }
)


_WS_RE = re.compile(r"\s+")


def normalize_key(message: str) -> str:
    """Normaliza el mensaje para usarlo como key del caché.

    - lowercase
    - strip leading/trailing whitespace
    - colapsa cualquier secuencia de whitespace a un solo espacio

    Así dos requests "Hola   mundo" y "hola mundo" comparten cache.
    """
    if not message:
        return ""
    return _WS_RE.sub(" ", message.strip()).lower()


@dataclass
class CachedResponse:
    """Snapshot guardado en el caché.

    Contiene exactamente lo que necesita el endpoint /chat para
    devolver una respuesta completa sin ejecutar el agent loop.
    """

    respuesta: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    provider_used: str | None = None
    tool_names_used: list[str] | None = None


class ResponseCache:
    """Cache TTL thread-safe para respuestas del harness."""

    def __init__(self, *, maxsize: int = 1000, ttl_seconds: int = 30) -> None:
        self._ttl_seconds = ttl_seconds
        self._maxsize = maxsize
        # cachetools.TTLCache NO es thread-safe; envolvemos en lock.
        self._cache: TTLCache[str, CachedResponse] = TTLCache(
            maxsize=maxsize, ttl=ttl_seconds
        )
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @staticmethod
    def make_key(tenant_slug: str, message: str) -> str:
        """Compone la key final: tenant + mensaje normalizado.

        Scoping por tenant evita que un cliente vea respuestas de otro.
        """
        return f"{tenant_slug}:{normalize_key(message)}"

    async def get(self, key: str) -> CachedResponse | None:
        async with self._lock:
            return self._cache.get(key)

    async def put(
        self,
        key: str,
        response: dict[str, Any] | CachedResponse,
        tool_names_used: list[str],
    ) -> bool:
        """Guarda la respuesta SOLO si ninguna tool usada está en la
        lista de write-tools. Devuelve True si efectivamente cachó.
        """
        if any(t in WRITE_TOOL_NAMES for t in tool_names_used):
            return False

        if isinstance(response, CachedResponse):
            entry = response
        else:
            entry = CachedResponse(
                respuesta=response.get("respuesta", ""),
                tokens_in=response.get("tokens_in"),
                tokens_out=response.get("tokens_out"),
                provider_used=response.get("provider_used"),
                tool_names_used=list(tool_names_used),
            )
        async with self._lock:
            self._cache[key] = entry
        return True

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    async def size(self) -> int:
        async with self._lock:
            return len(self._cache)


__all__ = [
    "CachedResponse",
    "ResponseCache",
    "WRITE_TOOL_NAMES",
    "normalize_key",
]
