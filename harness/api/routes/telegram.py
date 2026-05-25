"""POST /telegram/webhook — adapter Telegram → Agent.

Recibe los Updates de la Bot API de Telegram, los rutea al tenant
correcto (basado en `chat.id`/`from.id`) y contesta via `sendMessage`.

Reglas de oro:
- Siempre devolver 200 OK rápido. Telegram reintenta si pasamos de
  ~60s o devolvemos un 5xx → reintentos = mensajes duplicados al user.
- 401 SOLO si el header `X-Telegram-Bot-Api-Secret-Token` no matchea
  (sospecha de webhook falso) — Telegram NO reintenta 401.
- 503 si el bot no está configurado (`TELEGRAM_BOT_TOKEN` ausente).
- Cualquier error interno (provider exhausted, etc.) → contestamos al
  usuario por Telegram con mensaje amistoso y devolvemos 200.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request, status

from harness.adapters.telegram import (
    TelegramUpdate,
    get_bot_token,
    get_webhook_secret,
    resolve_telegram_tenant,
    send_message,
    validate_secret,
)
from harness.agent import Agent
from harness.api.deps import SessionStoreDep
from harness.api.routes.chat import _get_or_build_bundle
from harness.provider.router import ProvidersExhaustedError

log = logging.getLogger(__name__)

router = APIRouter()


# Timeout total del handler — Telegram tiene ~60s, dejamos margen para
# que el agent + sendMessage corran sin que se nos pase.
HANDLER_TIMEOUT_SECONDS = 25.0

# Mensajes canned (en español rioplatense).
MSG_NO_AUTORIZADO = (
    "No estás autorizado a usar este bot. "
    "Si pensás que es un error, contactá a Arnaldo."
)
MSG_ERROR_TEMPORAL = "Tuve un problema temporal, reintentá en unos segundos."
MSG_RESPUESTA_VACIA = "Disculpá, no pude generar respuesta."


def _session_id_for_chat(chat_id: int) -> str:
    """ID estable por chat de Telegram — así cada conversación tiene
    su propio historial persistido en `SessionStore`."""
    return f"telegram_chat_{chat_id}"


async def _run_agent_and_reply(
    request: Request,
    store,
    tenant_slug: str,
    chat_id: int,
    text: str,
    token: str,
) -> None:
    """Carga el bundle, corre el agent, persiste sesión y manda la
    respuesta al user via Telegram. Si algo explota → mensaje amistoso
    al user + log."""
    session_id = _session_id_for_chat(chat_id)
    try:
        bundle = _get_or_build_bundle(request, tenant_slug)
    except Exception as e:
        log.exception(
            "telegram_bundle_error",
            extra={"tenant": tenant_slug, "chat_id": chat_id, "err": str(e)},
        )
        await send_message(token, chat_id, MSG_ERROR_TEMPORAL)
        return

    prev_messages = store.load(tenant_slug, session_id)
    agent = Agent(provider=bundle.provider, tools=bundle.tools)
    agent.set_messages(prev_messages)

    try:
        respuesta = await agent.send(text)
    except ProvidersExhaustedError as e:
        log.error(
            "telegram_providers_exhausted",
            extra={"tenant": tenant_slug, "chat_id": chat_id, "err": str(e)},
        )
        await send_message(token, chat_id, MSG_ERROR_TEMPORAL)
        return
    except Exception as e:
        log.exception(
            "telegram_agent_unexpected_error",
            extra={"tenant": tenant_slug, "chat_id": chat_id, "err": str(e)},
        )
        await send_message(token, chat_id, MSG_ERROR_TEMPORAL)
        return

    # Persistimos el historial sólo si la ejecución llegó completa.
    store.save(tenant_slug, session_id, agent.messages)

    # Si el agent devolvió texto vacío, le decimos algo al usuario igual
    # — no nos podemos quedar mudos en un canal conversacional.
    reply = respuesta.strip() if respuesta else ""
    if not reply:
        reply = MSG_RESPUESTA_VACIA

    await send_message(token, chat_id, reply)


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    store: SessionStoreDep,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Recibe un Update de Telegram. SIEMPRE devolvemos 200 (excepto
    401 si el secret no matchea) — la respuesta al user va por
    `sendMessage`, no por el body de este response."""
    started = time.perf_counter()

    # 1. Validar el secret header (si está configurado).
    expected_secret = get_webhook_secret()
    if not validate_secret(x_telegram_bot_api_secret_token, expected_secret):
        log.warning(
            "telegram_webhook_secret_invalido",
            extra={"header_presente": bool(x_telegram_bot_api_secret_token)},
        )
        # 401 silencioso — Telegram no reintenta.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid secret",
        )

    # 2. Sin token de bot no podemos contestar nada — devolvemos 503
    # para que el operator se entere por monitoreo, sin reintento de
    # Telegram en bucle infinito.
    token = get_bot_token()
    if not token:
        log.error("telegram_webhook_sin_bot_token")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TELEGRAM_BOT_TOKEN no configurado",
        )

    # 3. Parsear el body — si el JSON está roto, devolvemos 200 igual
    # (no queremos bucles de reintento por payload malformado).
    try:
        raw = await request.json()
    except Exception as e:
        log.warning("telegram_webhook_json_invalido", extra={"err": str(e)})
        return {"ok": True}

    try:
        update = TelegramUpdate.model_validate_telegram(raw)
    except Exception as e:
        log.warning(
            "telegram_update_no_parsea",
            extra={"err": str(e), "raw_preview": str(raw)[:300]},
        )
        return {"ok": True}

    # 4. Sólo procesamos mensajes de texto. Callbacks, fotos, stickers,
    # voice, etc. → ignoramos silenciosamente (Fase 4+).
    if update.message is None or update.message.text is None:
        log.info(
            "telegram_update_no_text_ignorado",
            extra={"update_id": update.update_id},
        )
        return {"ok": True}

    chat_id = update.message.chat.id
    from_id = (
        update.message.from_user.id if update.message.from_user is not None else None
    )
    text = update.message.text.strip()
    if not text:
        log.info("telegram_text_vacio", extra={"chat_id": chat_id})
        return {"ok": True}

    # 5. Resolver tenant.
    tenant_slug = resolve_telegram_tenant(chat_id, from_id)
    if tenant_slug is None:
        log.warning(
            "telegram_chat_no_autorizado",
            extra={"chat_id": chat_id, "from_id": from_id},
        )
        await send_message(token, chat_id, MSG_NO_AUTORIZADO)
        return {"ok": True}

    # 6. Correr el agent con timeout — si nos pasamos del budget,
    # avisamos al user y cortamos limpio.
    try:
        await asyncio.wait_for(
            _run_agent_and_reply(
                request=request,
                store=store,
                tenant_slug=tenant_slug,
                chat_id=chat_id,
                text=text,
                token=token,
            ),
            timeout=HANDLER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.error(
            "telegram_webhook_timeout",
            extra={
                "tenant": tenant_slug,
                "chat_id": chat_id,
                "timeout_s": HANDLER_TIMEOUT_SECONDS,
            },
        )
        # Intento best-effort de avisar al user — si esto también
        # tarda, lo absorbemos abajo.
        try:
            await asyncio.wait_for(
                send_message(token, chat_id, MSG_ERROR_TEMPORAL),
                timeout=5.0,
            )
        except Exception:
            pass

    latency_ms = (time.perf_counter() - started) * 1000
    log.info(
        "telegram_webhook_ok",
        extra={
            "tenant": tenant_slug,
            "chat_id": chat_id,
            "update_id": update.update_id,
            "latency_ms": round(latency_ms, 1),
        },
    )
    return {"ok": True}
