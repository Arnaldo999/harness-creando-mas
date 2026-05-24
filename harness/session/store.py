"""SessionStore — guarda historial de Messages por session_id.

In-memory, TTL 1h, scoped por (tenant_slug, session_id).

Es deliberadamente simple — el cliente del CRM hoy NO envía session_id
(cada turno es one-shot via n8n). Cuando se quiera continuidad real,
basta con que el front mande session_id estable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from harness.api import Message


def new_session_id() -> str:
    """Genera un session_id fresco."""
    return f"sess_{uuid.uuid4().hex[:16]}"


@dataclass
class _Entry:
    messages: list[Message] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


class SessionStore:
    """Store in-memory simple con TTL."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._data: dict[tuple[str, str], _Entry] = {}

    def _key(self, tenant: str, session_id: str) -> tuple[str, str]:
        return (tenant, session_id)

    def _gc(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [k for k, v in self._data.items() if v.updated_at < cutoff]
        for k in stale:
            del self._data[k]

    def load(self, tenant: str, session_id: str) -> list[Message]:
        """Devuelve los messages persistidos. Lista vacía si no existe."""
        self._gc()
        entry = self._data.get(self._key(tenant, session_id))
        if entry is None:
            return []
        return list(entry.messages)

    def save(self, tenant: str, session_id: str, messages: list[Message]) -> None:
        self._gc()
        self._data[self._key(tenant, session_id)] = _Entry(
            messages=list(messages), updated_at=time.time()
        )

    def clear(self, tenant: str, session_id: str) -> None:
        self._data.pop(self._key(tenant, session_id), None)

    def __len__(self) -> int:
        return len(self._data)
