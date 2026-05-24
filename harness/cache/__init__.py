"""Cache de respuestas — TTL en memoria para queries idempotentes.

Skipea cache automáticamente si la conversación usó tools de escritura
(`update_lead_estado` y futuras). Solo cachea respuestas read-only.
"""

from harness.cache.store import (
    CachedResponse,
    ResponseCache,
    WRITE_TOOL_NAMES,
    normalize_key,
)

__all__ = [
    "CachedResponse",
    "ResponseCache",
    "WRITE_TOOL_NAMES",
    "normalize_key",
]
