"""Adapter Telegram → Agent.

Helpers puros (sin FastAPI) que usa `harness.api.routes.telegram`:

- `TelegramUpdate` y schemas anidados: subset del Update oficial de
  Telegram. Solo parseamos los campos que necesitamos para mensajes de
  texto 1-a-1 con el bot.
- `validate_secret(header, expected)`: compara con `secrets.compare_digest`
  para evitar timing attacks. Modo open dev (sin env): siempre True.
- `resolve_telegram_tenant(chat_id, from_id)`: scanea
  `tenants/<slug>/telegram_users.yaml` y devuelve el slug del tenant
  cuyo `allowed_chat_ids` contenga `chat_id` o `from_id`. None si nadie.
- `send_message(token, chat_id, text)`: POST async a la Bot API oficial.
  Ante error de red, log + raise — el caller decide qué hacer (en el
  webhook: log y devolver 200 OK igual, para que Telegram no reintente).

Diseño:
- 100% async, httpx.AsyncClient con timeout corto.
- Sin estado global — todos los helpers son stateless.
- Si el archivo `telegram_users.yaml` no existe en un tenant → ese
  tenant no acepta mensajes por Telegram (allowed_chat_ids = []).
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from harness.tenant.loader import _tenants_root, available_tenants

log = logging.getLogger(__name__)


# ---------- Pydantic schemas (subset del Update de Telegram) ----------


class TelegramUser(BaseModel):
    """`from` del mensaje (palabra reservada en Python → alias)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    is_bot: bool = False
    first_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    """Conversación donde llega el mensaje. Para 1-a-1 con el bot, `id`
    coincide con el user id; para grupos es negativo."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: Literal["private", "group", "supergroup", "channel"] = "private"


class TelegramMessage(BaseModel):
    """Mensaje entrante. Solo nos interesa el caso `text` — el resto
    (photo, sticker, voice, etc.) lo ignoramos a nivel webhook."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    chat: TelegramChat
    date: int
    text: str | None = None
    # `from` es palabra reservada en Python. Pydantic v2 maneja el alias.
    from_user: TelegramUser | None = None

    @classmethod
    def model_validate_telegram(cls, data: dict) -> "TelegramMessage":
        """Convierte `from` → `from_user` antes de validar."""
        if "from" in data and "from_user" not in data:
            data = {**data, "from_user": data["from"]}
        return cls.model_validate(data)


class TelegramUpdate(BaseModel):
    """Payload top-level del webhook. Ignoramos callback_query, etc.;
    solo procesamos `message`."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None

    @classmethod
    def model_validate_telegram(cls, data: dict) -> "TelegramUpdate":
        """Parseo defensivo: convierte el `from` anidado del mensaje
        antes de validar el update."""
        if isinstance(data.get("message"), dict):
            msg_data = data["message"]
            if "from" in msg_data and "from_user" not in msg_data:
                data = {
                    **data,
                    "message": {**msg_data, "from_user": msg_data["from"]},
                }
        return cls.model_validate(data)


# ---------- Helpers ----------


def validate_secret(header_value: str | None, expected: str | None) -> bool:
    """Compara el secret que Telegram manda en el header contra el del env.

    - Si `expected` (env `TELEGRAM_WEBHOOK_SECRET`) no está seteado → modo
      open dev, todo pasa (`True`).
    - Si está seteado y el header no llega o no matchea → `False`.
    - Comparamos con `secrets.compare_digest` para evitar timing attacks
      (consistente con `verify_bearer`).
    """
    if not expected:
        return True
    if not header_value:
        return False
    return secrets.compare_digest(header_value, expected)


def _load_telegram_allowed_ids(slug: str) -> list[int]:
    """Lee `tenants/<slug>/telegram_users.yaml`.

    Si el archivo no existe o está mal formado → lista vacía (ese
    tenant no acepta mensajes por Telegram).
    """
    path = _tenants_root() / slug / "telegram_users.yaml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        log.warning(
            "telegram_users_yaml_invalido",
            extra={"tenant": slug, "err": str(e)},
        )
        return []
    raw = data.get("allowed_chat_ids") or []
    # Coerción defensiva — Telegram chat_ids son int, pero alguien podría
    # escribirlos como string en el yaml.
    ids: list[int] = []
    for item in raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            log.warning(
                "telegram_allowed_id_no_entero",
                extra={"tenant": slug, "valor": repr(item)},
            )
    return ids


def resolve_telegram_tenant(chat_id: int, from_id: int | None = None) -> str | None:
    """Busca qué tenant tiene autorizado este chat/usuario.

    Recorre todos los tenants disponibles y devuelve el slug del primero
    cuyo `allowed_chat_ids` matchee con `chat_id` o `from_id`. Si nadie
    matchea → None.

    Match en orden alfabético de slugs — si el mismo chat_id está en dos
    tenants (no debería pasar en producción), gana el primero alfabético
    y logueamos warning.
    """
    matches: list[str] = []
    for slug in available_tenants():
        allowed = _load_telegram_allowed_ids(slug)
        if not allowed:
            continue
        if chat_id in allowed or (from_id is not None and from_id in allowed):
            matches.append(slug)

    if not matches:
        return None
    if len(matches) > 1:
        log.warning(
            "telegram_chat_id_en_multiples_tenants",
            extra={"chat_id": chat_id, "tenants": matches},
        )
    return matches[0]


async def send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "Markdown",
    timeout_seconds: float = 10.0,
) -> bool:
    """POST async a `https://api.telegram.org/bot<TOKEN>/sendMessage`.

    Devuelve True si Telegram aceptó el mensaje (status 200 + ok=true).
    Loggea y devuelve False ante cualquier error — el caller decide qué
    hacer (típicamente: log y seguir, no romper el webhook).

    `parse_mode='Markdown'`: ojo que Telegram NO admite todo el Markdown
    de OpenAI (sin tablas, sin code blocks anidados). Si rompe, podemos
    bajar a `None` (texto plano).
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        log.error(
            "telegram_send_http_error",
            extra={"chat_id": chat_id, "err": str(e)},
        )
        return False

    if resp.status_code != 200:
        log.error(
            "telegram_send_no_200",
            extra={
                "chat_id": chat_id,
                "status": resp.status_code,
                "body": resp.text[:500],
            },
        )
        return False

    body = resp.json()
    if not body.get("ok", False):
        log.error(
            "telegram_send_api_error",
            extra={
                "chat_id": chat_id,
                "description": body.get("description"),
                "error_code": body.get("error_code"),
            },
        )
        return False
    return True


def get_bot_token() -> str | None:
    """Token del bot (env var). None si no está seteado — el webhook
    debe rechazar requests con 503 en ese caso (no podemos contestar
    nada útil al usuario)."""
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def get_webhook_secret() -> str | None:
    """Secret opcional para validar el header `X-Telegram-Bot-Api-Secret-Token`."""
    return os.environ.get("TELEGRAM_WEBHOOK_SECRET")
