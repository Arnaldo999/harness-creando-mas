"""Memoria conversacional simple (in-memory, TTL 1h).

Fase 1 va a moverla a Postgres con tabla `agente_sessions`. Por ahora,
un dict global con expiración.
"""

from harness.session.store import SessionStore, new_session_id

__all__ = ["SessionStore", "new_session_id"]
