"""Tests del adapter de Telegram (Fase 3).

Mocks duros — NUNCA pegamos a `api.telegram.org` real. Stubeamos:
- `send_message` (parche al módulo de la ruta) para capturar lo que el
  webhook intentó mandar al usuario.
- `_get_or_build_bundle` (igual que tests del /chat) para meter un
  MockProvider en lugar de OpenAI/Postgres reales.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.adapters import telegram as telegram_adapter
from harness.api import Block, BlockType, Response, StopReason
from harness.api.app import create_app
from harness.api.routes import telegram as telegram_route
from harness.provider import MockProvider
from harness.tenant import TenantBundle
from harness.tenant.config import TenantConfig
from harness.tool.registry import Registry


# ---------- Helpers ----------


def _make_bundle(slug: str = "demo", reply: str = "Tenés 3 leads calientes.") -> TenantBundle:
    provider = MockProvider(
        [
            Response(
                content=[Block(type=BlockType.TEXT, text=reply)],
                stop_reason=StopReason.END_TURN,
            )
        ],
        provider_name="openai",
    )
    return TenantBundle(
        config=TenantConfig(slug=slug, system_prompt="test", tools_enabled=[]),
        provider=provider,
        tools=Registry(),
    )


def _valid_update(chat_id: int = 111222333, text: str = "Hola") -> dict[str, Any]:
    """Update mínimo que reproduce el formato real de Telegram."""
    return {
        "update_id": 999_888_777,
        "message": {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": chat_id,
                "is_bot": False,
                "first_name": "Arnaldo",
                "username": "arnaldo",
            },
            "text": text,
        },
    }


class _SendMessageRecorder:
    """Reemplazo de `send_message` que captura las llamadas en lugar de
    salir a la red. Se inyecta con monkeypatch al módulo de la ruta."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        token: str,
        chat_id: int,
        text: str,
        *,
        parse_mode: str = "Markdown",
        timeout_seconds: float = 10.0,
    ) -> bool:
        self.calls.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        return True


# ---------- Fixtures ----------


@pytest.fixture
def telegram_client(
    tenants_root_real: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, _SendMessageRecorder]:
    """App montada con bundle mock + `send_message` mockeado + bot token
    falso. Devuelve `(client, recorder)` para assertions."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN-FOR-TESTS")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    # El handler de Telegram importa `_get_or_build_bundle` desde
    # `chat.py` con `from ... import ...`, así que el patch tiene que
    # apuntar al símbolo dentro del módulo `telegram_route`.
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    return TestClient(app), recorder


@pytest.fixture
def tenant_demo_con_arnaldo_autorizado(
    tenants_root_real: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> int:
    """Crea un tenants root temporal con `demo/` que autoriza al chat_id
    111222333. Devuelve el chat_id autorizado."""
    chat_id = 111_222_333
    root = tmp_path / "tenants"
    demo = root / "demo"
    demo.mkdir(parents=True)
    (demo / "system_prompt.md").write_text("test", encoding="utf-8")
    (demo / "tools.yaml").write_text("enabled: []\n", encoding="utf-8")
    (demo / "data_sources.yaml").write_text("{}\n", encoding="utf-8")
    (demo / "telegram_users.yaml").write_text(
        f"allowed_chat_ids: [{chat_id}]\n", encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))
    return chat_id


# ---------- validate_secret ----------


def test_validate_secret_correcto() -> None:
    assert telegram_adapter.validate_secret("mi-secret", "mi-secret") is True


def test_validate_secret_incorrecto() -> None:
    assert telegram_adapter.validate_secret("otro", "mi-secret") is False


def test_validate_secret_sin_env() -> None:
    # Sin secret configurado → modo dev open, todo pasa.
    assert telegram_adapter.validate_secret(None, None) is True
    assert telegram_adapter.validate_secret("cualquier-cosa", None) is True


def test_validate_secret_header_ausente_con_secret_configurado() -> None:
    assert telegram_adapter.validate_secret(None, "mi-secret") is False


# ---------- resolve_telegram_tenant ----------


def test_resolve_tenant_match(
    tenant_demo_con_arnaldo_autorizado: int,
) -> None:
    chat_id = tenant_demo_con_arnaldo_autorizado
    assert telegram_adapter.resolve_telegram_tenant(chat_id) == "demo"


def test_resolve_tenant_match_via_from_id(
    tenant_demo_con_arnaldo_autorizado: int,
) -> None:
    from_id = tenant_demo_con_arnaldo_autorizado
    # chat_id distinto pero from_id en allowed → debe matchear.
    assert telegram_adapter.resolve_telegram_tenant(999, from_id) == "demo"


def test_resolve_tenant_no_match(
    tenant_demo_con_arnaldo_autorizado: int,
) -> None:
    # chat_id distinto del autorizado → None.
    assert telegram_adapter.resolve_telegram_tenant(42, 42) is None


def test_resolve_tenant_sin_yaml(
    tenants_root_real: Path,
) -> None:
    # En el repo, demo/telegram_users.yaml tiene allowed_chat_ids vacío
    # → ningún match.
    assert telegram_adapter.resolve_telegram_tenant(111) is None


# ---------- Endpoint /telegram/webhook ----------


def test_webhook_endpoint_authorized(
    monkeypatch: pytest.MonkeyPatch,
    tenant_demo_con_arnaldo_autorizado: int,
) -> None:
    """chat_id autorizado → 200 OK y send_message invocado con la
    respuesta real del agent."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    # El handler de Telegram importa `_get_or_build_bundle` desde
    # `chat.py` con `from ... import ...`, así que el patch tiene que
    # apuntar al símbolo dentro del módulo `telegram_route`.
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    chat_id = tenant_demo_con_arnaldo_autorizado
    r = client.post(
        "/telegram/webhook",
        json=_valid_update(chat_id=chat_id, text="¿Cuántos leads tengo?"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["chat_id"] == chat_id
    assert call["text"] == "Tenés 3 leads calientes."
    assert call["token"] == "FAKE-TOKEN"


def test_webhook_endpoint_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    tenants_root_real: Path,
) -> None:
    """chat_id no en lista → 200 OK pero el mensaje al user es 'no autorizado'."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    # El handler de Telegram importa `_get_or_build_bundle` desde
    # `chat.py` con `from ... import ...`, así que el patch tiene que
    # apuntar al símbolo dentro del módulo `telegram_route`.
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    # Chat_id ajeno (no autorizado por ningún tenant — el demo/ del repo
    # tiene allowed_chat_ids vacío).
    r = client.post("/telegram/webhook", json=_valid_update(chat_id=42, text="hola"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["chat_id"] == 42
    assert "No estás autorizado" in recorder.calls[0]["text"]


def test_webhook_endpoint_bad_secret(
    monkeypatch: pytest.MonkeyPatch,
    tenants_root_real: Path,
) -> None:
    """Header secret incorrecto → 401, send_message NO se llama."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "el-secret-bueno")
    # El handler de Telegram importa `_get_or_build_bundle` desde
    # `chat.py` con `from ... import ...`, así que el patch tiene que
    # apuntar al símbolo dentro del módulo `telegram_route`.
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    r = client.post(
        "/telegram/webhook",
        json=_valid_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "el-secret-MALO"},
    )
    assert r.status_code == 401
    assert recorder.calls == []


def test_webhook_endpoint_secret_correcto_pasa(
    monkeypatch: pytest.MonkeyPatch,
    tenant_demo_con_arnaldo_autorizado: int,
) -> None:
    """Con el secret correcto, el flujo procede normalmente."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "el-secret-bueno")
    # El handler de Telegram importa `_get_or_build_bundle` desde
    # `chat.py` con `from ... import ...`, así que el patch tiene que
    # apuntar al símbolo dentro del módulo `telegram_route`.
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    chat_id = tenant_demo_con_arnaldo_autorizado
    r = client.post(
        "/telegram/webhook",
        json=_valid_update(chat_id=chat_id),
        headers={"X-Telegram-Bot-Api-Secret-Token": "el-secret-bueno"},
    )
    assert r.status_code == 200
    assert len(recorder.calls) == 1


def test_webhook_endpoint_non_text_message(
    monkeypatch: pytest.MonkeyPatch,
    tenants_root_real: Path,
) -> None:
    """Update con `photo` en vez de `text` → 200 silencioso, sin send_message,
    sin invocar al agent (lo verificamos porque _get_or_build_bundle NO se llama)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    bundle_call_count = {"n": 0}

    def _counting_bundle(request: Any, slug: str) -> TenantBundle:
        bundle_call_count["n"] += 1
        return _make_bundle(slug)

    monkeypatch.setattr(telegram_route, "_get_or_build_bundle", _counting_bundle)
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    # Update sin `text`, con `photo` (típico de un usuario que sube una foto).
    update_with_photo = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False},
            "photo": [{"file_id": "ABC", "file_unique_id": "x", "width": 90, "height": 90}],
        },
    }
    r = client.post("/telegram/webhook", json=update_with_photo)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert recorder.calls == []
    assert bundle_call_count["n"] == 0


def test_webhook_endpoint_sin_bot_token_503(
    monkeypatch: pytest.MonkeyPatch,
    tenants_root_real: Path,
) -> None:
    """Si TELEGRAM_BOT_TOKEN no está → 503 (no podemos contestarle al user)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    app = create_app()
    client = TestClient(app)

    r = client.post("/telegram/webhook", json=_valid_update())
    assert r.status_code == 503


def test_webhook_endpoint_callback_query_ignorado(
    monkeypatch: pytest.MonkeyPatch,
    tenants_root_real: Path,
) -> None:
    """Update con `callback_query` (sin `message`) → 200 silencioso."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    r = client.post(
        "/telegram/webhook",
        json={"update_id": 2, "callback_query": {"id": "x", "from": {"id": 1}}},
    )
    assert r.status_code == 200
    assert recorder.calls == []
