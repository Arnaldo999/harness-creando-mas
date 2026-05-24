"""POST /chat — endpoint primario.

Drop-in replacement del webhook n8n actual:
    POST n8n.lovbot.ai/webhook/crm-ia-chat
    body: {message, tenant_slug, session_id}
    resp: {respuesta, ok}

Acá extendemos la response con tokens + provider_used + session_id real.

Flujo:
1. Auth (require_bearer).
2. Resolver tenant_slug → cargar TenantBundle (cacheado en app.state).
3. Cargar Messages previos del SessionStore (si vienen con session_id).
4. Correr Agent.send(message) — async.
5. Persistir messages back al SessionStore.
6. Devolver ChatResponse con provider_used desde el ProviderRouter.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request, status

from harness.agent import Agent
from harness.api.deps import BearerAuth, SessionStoreDep
from harness.api.schemas import ChatRequest, ChatResponse
from harness.provider.router import ProviderRouter, ProvidersExhaustedError
from harness.session import new_session_id
from harness.tenant import (
    TenantBundle,
    TenantNotFoundError,
    build_tenant_bundle,
    load_tenant_config,
    resolve_tenant,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _get_or_build_bundle(request: Request, slug: str) -> TenantBundle:
    """TenantBundles se cachean en `app.state.tenant_bundles` para no
    recrear pools de Postgres por request.
    """
    bundles: dict[str, TenantBundle] = getattr(request.app.state, "tenant_bundles", {})
    if slug not in bundles:
        config = load_tenant_config(slug)
        bundles[slug] = build_tenant_bundle(config)
        request.app.state.tenant_bundles = bundles
    return bundles[slug]


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    store: SessionStoreDep,
    _auth: BearerAuth,
) -> ChatResponse:
    started = time.perf_counter()

    # 1. Tenant.
    try:
        slug = resolve_tenant(req.tenant_slug)
    except TenantNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    bundle = _get_or_build_bundle(request, slug)

    # 2. Session.
    session_id = req.session_id or new_session_id()
    prev_messages = store.load(slug, session_id)

    # 3. Agent.
    agent = Agent(provider=bundle.provider, tools=bundle.tools)
    agent.set_messages(prev_messages)

    try:
        respuesta = await agent.send(req.message)
    except ProvidersExhaustedError as e:
        log.error("providers_exhausted", extra={"tenant": slug, "err": str(e)})
        return ChatResponse(
            respuesta=(
                "Disculpá, el asistente está temporalmente fuera. "
                "Reintentá en unos segundos."
            ),
            ok=False,
            session_id=session_id,
            provider_used=None,
        )
    except Exception as e:
        log.exception("chat_unexpected_error", extra={"tenant": slug, "err": str(e)})
        return ChatResponse(
            respuesta="Disculpá, ocurrió un error inesperado procesando tu consulta.",
            ok=False,
            session_id=session_id,
            provider_used=None,
        )

    # 4. Persistir messages.
    store.save(slug, session_id, agent.messages)

    # 5. Armar response.
    provider_used: str | None = None
    if isinstance(bundle.provider, ProviderRouter):
        provider_used = bundle.provider.last_used
    else:
        provider_used = bundle.provider.name

    usage = agent.usage
    latency_ms = (time.perf_counter() - started) * 1000
    log.info(
        "chat_ok",
        extra={
            "tenant": slug,
            "session_id": session_id,
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "provider_used": provider_used,
            "latency_ms": round(latency_ms, 1),
        },
    )

    return ChatResponse(
        respuesta=respuesta,
        ok=True,
        tokens_in=usage.input_tokens or None,
        tokens_out=usage.output_tokens or None,
        provider_used=provider_used,
        session_id=session_id,
    )
