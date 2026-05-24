"""POST /chat — endpoint primario.
POST /chat/stream — variante SSE.

Drop-in replacement del webhook n8n actual:
    POST n8n.lovbot.ai/webhook/crm-ia-chat
    body: {message, tenant_slug, session_id}
    resp: {respuesta, ok}

Extendemos la response con tokens + provider_used + session_id + cached.

Flujo /chat:
1. Auth (require_bearer).
2. Resolver tenant_slug → cargar TenantBundle (cacheado en app.state).
3. CACHE: si hit por (tenant, msg normalizado) → return inmediato.
4. Cargar Messages previos del SessionStore (si vienen con session_id).
5. Correr Agent.send(message) — async.
6. Persistir messages back al SessionStore.
7. Guardar en cache (skip si se usó write-tool).
8. Devolver ChatResponse con provider_used desde el ProviderRouter.

Flujo /chat/stream:
- Mismo pero emite SSE: `event: text|tool_start|tool_end|done|error`.
- Si hay cache hit → emite UN `event: text` con la respuesta entera
  y `event: done` con `cached: true`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from harness.agent import Agent
from harness.api.deps import BearerAuth, SessionStoreDep
from harness.api.schemas import ChatRequest, ChatResponse
from harness.cache import ResponseCache
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


def _get_response_cache(request: Request) -> ResponseCache:
    """ResponseCache vive en app.state. Si no existe (tests viejos),
    lo creamos lazy con defaults."""
    cache = getattr(request.app.state, "response_cache", None)
    if cache is None:
        cache = ResponseCache()
        request.app.state.response_cache = cache
    return cache


def _resolve_provider_name(bundle: TenantBundle) -> str | None:
    if isinstance(bundle.provider, ProviderRouter):
        return bundle.provider.last_used
    return bundle.provider.name


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
    cache = _get_response_cache(request)

    # 2. Session.
    session_id = req.session_id or new_session_id()

    # 3. Cache lookup. Si hit → response inmediato.
    cache_key = ResponseCache.make_key(slug, req.message)
    hit = await cache.get(cache_key)
    if hit is not None:
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "chat_cache_hit",
            extra={
                "tenant": slug,
                "session_id": session_id,
                "latency_ms": round(latency_ms, 2),
            },
        )
        return ChatResponse(
            respuesta=hit.respuesta,
            ok=True,
            tokens_in=hit.tokens_in,
            tokens_out=hit.tokens_out,
            provider_used=hit.provider_used,
            session_id=session_id,
            cached=True,
        )

    # 4. Cargar historial.
    prev_messages = store.load(slug, session_id)

    # 5. Agent.
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

    # 6. Persistir messages.
    store.save(slug, session_id, agent.messages)

    # 7. Armar response.
    provider_used = _resolve_provider_name(bundle)
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
            "tools_used": agent.tool_names_used,
        },
    )

    response = ChatResponse(
        respuesta=respuesta,
        ok=True,
        tokens_in=usage.input_tokens or None,
        tokens_out=usage.output_tokens or None,
        provider_used=provider_used,
        session_id=session_id,
        cached=False,
    )

    # 8. Cache (skip si hubo write-tool).
    await cache.put(
        cache_key,
        {
            "respuesta": respuesta,
            "tokens_in": usage.input_tokens or None,
            "tokens_out": usage.output_tokens or None,
            "provider_used": provider_used,
        },
        agent.tool_names_used,
    )

    return response


# ---------- SSE ----------

def _sse_format(event: str, data: dict) -> str:
    """Formato wire de Server-Sent Events.

    `event:` + `data:` (JSON serializado en una línea para que el
    parser de EventSource lo reconozca como un único evento) + blank line.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    store: SessionStoreDep,
    _auth: BearerAuth,
) -> StreamingResponse:
    """Variante SSE de /chat.

    Protocolo de eventos:
    - `text` {content}: delta de texto.
    - `tool_start` {tool, step}: arranca una herramienta.
    - `tool_end` {tool, step, ms}: termina una herramienta.
    - `done` {tokens_in, tokens_out, provider_used, session_id, latency_ms, cached}.
    - `error` {message, ok=false}.

    Se emite UN solo terminal: `done` o `error`.
    """
    started = time.perf_counter()

    try:
        slug = resolve_tenant(req.tenant_slug)
    except TenantNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    bundle = _get_or_build_bundle(request, slug)
    cache = _get_response_cache(request)
    session_id = req.session_id or new_session_id()
    cache_key = ResponseCache.make_key(slug, req.message)

    async def event_generator() -> AsyncIterator[str]:
        # --- Cache hit ---
        hit = await cache.get(cache_key)
        if hit is not None:
            latency_ms = (time.perf_counter() - started) * 1000
            yield _sse_format("text", {"content": hit.respuesta})
            yield _sse_format(
                "done",
                {
                    "tokens_in": hit.tokens_in,
                    "tokens_out": hit.tokens_out,
                    "provider_used": hit.provider_used,
                    "session_id": session_id,
                    "latency_ms": round(latency_ms, 2),
                    "cached": True,
                },
            )
            log.info(
                "chat_stream_cache_hit",
                extra={"tenant": slug, "session_id": session_id},
            )
            return

        # --- Stream real ---
        prev_messages = store.load(slug, session_id)
        agent = Agent(provider=bundle.provider, tools=bundle.tools)
        agent.set_messages(prev_messages)

        # Acumulamos el texto que se le envía al cliente para poder
        # guardarlo en caché al final.
        full_text_parts: list[str] = []
        step = 0

        try:
            async for ev in agent.send_stream(req.message):
                # Disconnect → cortar limpio.
                if await request.is_disconnected():
                    log.info(
                        "chat_stream_client_disconnect",
                        extra={"tenant": slug, "session_id": session_id},
                    )
                    return

                if ev.type == "text":
                    if ev.text:
                        full_text_parts.append(ev.text)
                        yield _sse_format("text", {"content": ev.text})
                elif ev.type == "tool_use_start":
                    step += 1
                    yield _sse_format(
                        "tool_start",
                        {"tool": ev.tool_name, "step": step},
                    )
                elif ev.type == "tool_use_complete":
                    yield _sse_format(
                        "tool_end",
                        {
                            "tool": ev.tool_name,
                            "step": step,
                            "ms": ev.latency_ms,
                        },
                    )
                elif ev.type == "stop":
                    # Stop natural del agent — el done lo emitimos abajo
                    # después de persistir todo.
                    pass
                elif ev.type == "error":
                    yield _sse_format(
                        "error", {"message": ev.text or "error desconocido", "ok": False}
                    )
                    return
        except ProvidersExhaustedError as e:
            log.error(
                "providers_exhausted_stream",
                extra={"tenant": slug, "err": str(e)},
            )
            yield _sse_format(
                "error",
                {
                    "message": (
                        "Disculpá, el asistente está temporalmente fuera. "
                        "Reintentá en unos segundos."
                    ),
                    "ok": False,
                },
            )
            return
        except Exception as e:
            log.exception(
                "chat_stream_unexpected_error",
                extra={"tenant": slug, "err": str(e)},
            )
            yield _sse_format(
                "error",
                {"message": "Error inesperado procesando tu consulta.", "ok": False},
            )
            return

        # Persistir messages.
        store.save(slug, session_id, agent.messages)

        provider_used = _resolve_provider_name(bundle)
        usage = agent.usage
        latency_ms = (time.perf_counter() - started) * 1000
        full_text = "".join(full_text_parts)

        # Cache (skip si hubo write-tool).
        await cache.put(
            cache_key,
            {
                "respuesta": full_text,
                "tokens_in": usage.input_tokens or None,
                "tokens_out": usage.output_tokens or None,
                "provider_used": provider_used,
            },
            agent.tool_names_used,
        )

        yield _sse_format(
            "done",
            {
                "tokens_in": usage.input_tokens or None,
                "tokens_out": usage.output_tokens or None,
                "provider_used": provider_used,
                "session_id": session_id,
                "latency_ms": round(latency_ms, 1),
                "cached": False,
            },
        )
        log.info(
            "chat_stream_ok",
            extra={
                "tenant": slug,
                "session_id": session_id,
                "tokens_in": usage.input_tokens,
                "tokens_out": usage.output_tokens,
                "provider_used": provider_used,
                "latency_ms": round(latency_ms, 1),
                "tools_used": agent.tool_names_used,
            },
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Importante en nginx/Coolify: deshabilita buffering proxy.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
