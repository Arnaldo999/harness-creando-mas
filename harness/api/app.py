"""Factory `create_app()` de la app FastAPI.

Uso:
    # Dev local:
    uvicorn harness.api.app:create_app --factory --reload --port 8000

    # Prod (Dockerfile):
    uvicorn harness.api.app:create_app --factory --host 0.0.0.0 --port 8000 --workers 2

Configura:
- Logging básico (LOG_LEVEL).
- Routers (/health, /chat).
- CORS para crm.lovbot.ai + localhost.
- `app.state.session_store` y `app.state.tenant_bundles` (vacío al boot —
  los bundles se construyen lazy on first request por tenant).
- Carga `.env` si existe (python-dotenv).
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness import __version__
from harness.api.routes import chat as chat_route
from harness.api.routes import health as health_route
from harness.api.routes import telegram as telegram_route
from harness.cache import ResponseCache
from harness.limits import RateLimiter
from harness.session import SessionStore
from harness.tenant.auth import get_required_bearer_token

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _cors_origins() -> list[str]:
    custom = os.environ.get("CORS_ALLOW_ORIGINS")
    if custom:
        return [o.strip() for o in custom.split(",") if o.strip()]
    return [
        "https://crm.lovbot.ai",
        "http://localhost",
        "http://localhost:8080",
        "http://localhost:3000",
    ]


def create_app() -> FastAPI:
    load_dotenv()  # noop si no hay .env
    _configure_logging()

    app = FastAPI(
        title="harness-creando-mas",
        description=(
            "HTTP harness multi-tenant de la agencia Creando Más. "
            "Fase 0: demo inmobiliario Lovbot (replaces n8n.lovbot.ai/webhook/crm-ia-chat)."
        ),
        version=__version__,
    )

    # CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # State.
    app.state.session_store = SessionStore()
    app.state.tenant_bundles = {}
    # ResponseCache: TTL configurable via env vars. Defaults razonables
    # para el CRM (30s — el estado del CRM cambia rápido).
    cache_ttl = int(os.environ.get("HARNESS_CACHE_TTL_SECONDS", "30"))
    cache_size = int(os.environ.get("HARNESS_CACHE_MAXSIZE", "1000"))
    app.state.response_cache = ResponseCache(
        maxsize=cache_size, ttl_seconds=cache_ttl
    )
    # RateLimiter: una sola instancia para todos los tenants. El scoping
    # se hace via la key (tenant:user). Maxsize alto: cap de memoria duro
    # sin acotar el ecosistema esperado de chats.
    rl_maxsize = int(os.environ.get("HARNESS_RATELIMIT_MAXSIZE", "10000"))
    app.state.rate_limiter = RateLimiter(maxsize=rl_maxsize)

    # Routers.
    app.include_router(health_route.router, tags=["meta"])
    app.include_router(chat_route.router, tags=["chat"])
    app.include_router(telegram_route.router, tags=["telegram"])

    # Warn si arrancamos sin auth.
    if not get_required_bearer_token():
        log.warning(
            "auth_disabled: LOVBOT_AGENTE_API_KEY no seteado. "
            "El endpoint /chat acepta requests SIN bearer token. "
            "OK para dev local, NO seguro para producción."
        )

    return app
