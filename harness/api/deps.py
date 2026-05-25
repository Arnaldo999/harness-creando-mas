"""Dependencias compartidas de FastAPI (auth, tenant resolution, session store).

Inyectadas en los routers via `Depends(...)`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from harness.limits import RateLimiter
from harness.session import SessionStore
from harness.tenant.auth import verify_bearer


def get_session_store(request: Request) -> SessionStore:
    """SessionStore vive en `app.state.session_store`, inicializado por
    el factory `create_app()`.
    """
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        store = SessionStore()
        request.app.state.session_store = store
    return store


def get_rate_limiter(request: Request) -> RateLimiter:
    """RateLimiter vive en `app.state.rate_limiter`, inicializado por
    el factory. Lazy-fallback igual que SessionStore para tests
    antiguos que armen la app a mano.
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = RateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


def require_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Valida el header `Authorization: Bearer <token>` si el env var
    `LOVBOT_AGENTE_API_KEY` está seteado. En modo dev (sin env), todo pasa.
    """
    token: str | None = None
    if authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    if not verify_bearer(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token inválido o ausente",
        )


# Alias re-exportado para legibilidad en routers.
BearerAuth = Annotated[None, Depends(require_bearer)]
SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
