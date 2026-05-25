"""Tests del rate limiter per-tenant.

Cubren tres capas:
1. Unit del `RateLimiter` (counters, ventanas, thread-safety).
2. Loader: el block `rate_limits` del yaml se parsea bien.
3. Integración E2E: `/chat`, `/chat/stream`, `/telegram/webhook`
   devuelven los mensajes de corte cuando se excede.

NO pegamos a Telegram/OpenAI reales — usamos MockProvider + recorder.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.adapters import telegram as telegram_adapter  # noqa: F401  (asegurar import)
from harness.api import Block, BlockType, Response, StopReason, StreamEvent, Usage
from harness.api.app import create_app
from harness.api.routes import chat as chat_route
from harness.api.routes import telegram as telegram_route
from harness.limits import (
    RateLimiter,
    RateLimitsConfig,
    format_retry_after_human,
    format_window_human,
)
from harness.provider import MockProvider
from harness.tenant import TenantBundle
from harness.tenant.config import TenantConfig
from harness.tenant.loader import load_tenant_config
from harness.tool.registry import Registry


# ---------------------------------------------------------------------------
# Helpers locales
# ---------------------------------------------------------------------------


def _make_bundle(
    slug: str = "demo",
    reply: str = "Tenés 3 leads calientes.",
    limits: RateLimitsConfig | None = None,
) -> TenantBundle:
    provider = MockProvider(
        [
            Response(
                content=[Block(type=BlockType.TEXT, text=reply)],
                stop_reason=StopReason.END_TURN,
            )
        ]
        * 20,  # responses de sobra para los tests que mandan varias requests
        provider_name="openai",
    )
    return TenantBundle(
        config=TenantConfig(
            slug=slug,
            system_prompt="test",
            tools_enabled=[],
            rate_limits=limits,
        ),
        provider=provider,
        tools=Registry(),
    )


def _make_streaming_bundle(
    slug: str = "demo", limits: RateLimitsConfig | None = None
) -> TenantBundle:
    events = [
        [
            StreamEvent(type="text", text="Hola"),
            StreamEvent(type="text", text=" mundo"),
            StreamEvent(
                type="stop",
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=10, output_tokens=2),
            ),
        ]
    ] * 20
    provider = MockProvider(provider_name="openai", stream_events=events)
    return TenantBundle(
        config=TenantConfig(
            slug=slug,
            system_prompt="test",
            tools_enabled=[],
            rate_limits=limits,
        ),
        provider=provider,
        tools=Registry(),
    )


def _parse_sse(body: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            if current_event is not None:
                events.append((current_event, "\n".join(current_data)))
                current_event = None
                current_data = []
            continue
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:") :].strip())
    if current_event is not None:
        events.append((current_event, "\n".join(current_data)))
    return events


def _valid_update(chat_id: int = 111_222_333, text: str = "Hola") -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Unit: RateLimiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sin_limits_pasa_siempre() -> None:
    """Config sin ningún límite seteado → siempre allowed, current vacío."""
    rl = RateLimiter()
    limits = RateLimitsConfig()  # todos None

    for _ in range(100):
        result = await rl.check_and_record("tenant:user", limits)
        assert result.allowed is True
        assert result.exceeded_window is None
        assert result.current == {}


@pytest.mark.asyncio
async def test_per_minute_corta() -> None:
    rl = RateLimiter()
    limits = RateLimitsConfig(per_minute=5)

    # Primeros 5: pasan.
    for i in range(5):
        result = await rl.check_and_record("t:u", limits)
        assert result.allowed is True, f"hit {i + 1} debería pasar"
        assert result.current["minute"] == i + 1

    # El 6° corta.
    result = await rl.check_and_record("t:u", limits)
    assert result.allowed is False
    assert result.exceeded_window == "minute"
    assert result.retry_after_seconds is not None
    assert 1 <= result.retry_after_seconds <= 60
    # Y no incrementó el counter (5, no 6).
    assert result.current["minute"] == 5


@pytest.mark.asyncio
async def test_per_day_corta_independiente() -> None:
    """Con per_day=20 y sin per_minute → 21° request cae con
    exceeded_window='day'."""
    rl = RateLimiter()
    limits = RateLimitsConfig(per_day=20)

    for i in range(20):
        result = await rl.check_and_record("t:u", limits)
        assert result.allowed is True, f"hit {i + 1} debería pasar"

    result = await rl.check_and_record("t:u", limits)
    assert result.allowed is False
    assert result.exceeded_window == "day"
    # Retry hasta ~24h.
    assert result.retry_after_seconds is not None
    assert 1 <= result.retry_after_seconds <= 86400


@pytest.mark.asyncio
async def test_keys_son_independientes() -> None:
    """tenant1:userA en límite no afecta a tenant1:userB."""
    rl = RateLimiter()
    limits = RateLimitsConfig(per_minute=2)

    # userA usa sus 2.
    for _ in range(2):
        assert (await rl.check_and_record("t1:userA", limits)).allowed is True
    # userA bloqueado.
    blocked = await rl.check_and_record("t1:userA", limits)
    assert blocked.allowed is False

    # userB sigue libre.
    for _ in range(2):
        assert (await rl.check_and_record("t1:userB", limits)).allowed is True


@pytest.mark.asyncio
async def test_thread_safe() -> None:
    """50 requests concurrentes con limit=10 → exactamente 10 allowed."""
    rl = RateLimiter()
    limits = RateLimitsConfig(per_minute=10)

    async def _hit() -> bool:
        result = await rl.check_and_record("t:u", limits)
        return result.allowed

    results = await asyncio.gather(*[_hit() for _ in range(50)])
    allowed_count = sum(1 for r in results if r)
    assert allowed_count == 10, f"esperaba 10 allowed, conté {allowed_count}"


@pytest.mark.asyncio
async def test_retry_after_minute() -> None:
    """Cuando exceeded en minute, retry_after entre 1 y 60 segundos."""
    rl = RateLimiter()
    limits = RateLimitsConfig(per_minute=1)

    # Primer hit pasa.
    first = await rl.check_and_record("t:u", limits)
    assert first.allowed is True
    # Segundo cae con retry razonable.
    blocked = await rl.check_and_record("t:u", limits)
    assert blocked.allowed is False
    assert blocked.exceeded_window == "minute"
    assert blocked.retry_after_seconds is not None
    assert 1 <= blocked.retry_after_seconds <= 60


@pytest.mark.asyncio
async def test_orden_de_chequeo_minute_primero() -> None:
    """Si tanto minute como hour exceden, reportamos minute (chequeo
    primero, mensaje más útil para el user)."""
    rl = RateLimiter()
    # Límites simétricos absurdos: agotar el minuto también agota la hora.
    limits = RateLimitsConfig(per_minute=2, per_hour=2)

    await rl.check_and_record("t:u", limits)
    await rl.check_and_record("t:u", limits)
    blocked = await rl.check_and_record("t:u", limits)
    assert blocked.allowed is False
    assert blocked.exceeded_window == "minute"


@pytest.mark.asyncio
async def test_reset_limpia_counters() -> None:
    rl = RateLimiter()
    limits = RateLimitsConfig(per_minute=1)

    assert (await rl.check_and_record("t:u", limits)).allowed is True
    assert (await rl.check_and_record("t:u", limits)).allowed is False

    await rl.reset("t:u")
    # Reset → vuelve a pasar el primer hit.
    assert (await rl.check_and_record("t:u", limits)).allowed is True


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def test_format_retry_after_human() -> None:
    assert format_retry_after_human(30) == "30 segundos"
    assert format_retry_after_human(1) == "1 segundo"
    assert format_retry_after_human(60) == "1 minuto"
    assert format_retry_after_human(61) == "2 minutos"  # redondeo arriba
    assert format_retry_after_human(720) == "12 minutos"
    assert format_retry_after_human(3600) == "1 hora"
    assert format_retry_after_human(82_800) == "23 horas"
    assert format_retry_after_human(86_400) == "1 día"
    assert format_retry_after_human(0) == "unos segundos"
    assert format_retry_after_human(None) == "unos segundos"


def test_format_window_human() -> None:
    assert format_window_human("minute") == "último minuto"
    assert format_window_human("hour") == "última hora"
    assert format_window_human("day") == "último día"
    assert format_window_human(None) == "ventana actual"


# ---------------------------------------------------------------------------
# Loader: parsea bien el block del yaml
# ---------------------------------------------------------------------------


def test_loader_lee_rate_limits_del_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tenant con block `rate_limits` válido → TenantConfig.rate_limits
    se popula con los valores correctos."""
    root = tmp_path / "tenants"
    demo = root / "test-tenant"
    demo.mkdir(parents=True)
    (demo / "system_prompt.md").write_text("test", encoding="utf-8")
    (demo / "tools.yaml").write_text("enabled: []\n", encoding="utf-8")
    (demo / "data_sources.yaml").write_text(
        "rate_limits:\n  per_minute: 7\n  per_hour: 42\n  per_day: 99\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))

    cfg = load_tenant_config("test-tenant")
    assert cfg.rate_limits is not None
    assert cfg.rate_limits.per_minute == 7
    assert cfg.rate_limits.per_hour == 42
    assert cfg.rate_limits.per_day == 99


def test_loader_sin_block_rate_limits_es_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tenant sin block `rate_limits` → rate_limits = None (legacy)."""
    root = tmp_path / "tenants"
    demo = root / "sin-limits"
    demo.mkdir(parents=True)
    (demo / "system_prompt.md").write_text("test", encoding="utf-8")
    (demo / "tools.yaml").write_text("enabled: []\n", encoding="utf-8")
    (demo / "data_sources.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))

    cfg = load_tenant_config("sin-limits")
    assert cfg.rate_limits is None


def test_loader_rate_limit_invalido_se_ignora(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Yaml con valores raros (string, negativo, cero) → el campo se
    deja en None pero NO crashea el loader."""
    root = tmp_path / "tenants"
    demo = root / "raro"
    demo.mkdir(parents=True)
    (demo / "system_prompt.md").write_text("test", encoding="utf-8")
    (demo / "tools.yaml").write_text("enabled: []\n", encoding="utf-8")
    (demo / "data_sources.yaml").write_text(
        "rate_limits:\n"
        "  per_minute: abc\n"   # string roto
        "  per_hour: -5\n"       # negativo
        "  per_day: 0\n",        # cero
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))

    cfg = load_tenant_config("raro")
    assert cfg.rate_limits is not None
    assert cfg.rate_limits.per_minute is None
    assert cfg.rate_limits.per_hour is None
    assert cfg.rate_limits.per_day is None


# ---------------------------------------------------------------------------
# E2E: /chat
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_client_con_limit_2_por_minuto(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """App con tenant que tiene per_minute=2."""
    limits = RateLimitsConfig(per_minute=2)
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug, limits=limits),
    )
    app = create_app()
    return TestClient(app)


def test_chat_endpoint_rate_limited(
    chat_client_con_limit_2_por_minuto: TestClient,
) -> None:
    """Configurar tenant con per_minute=2, hacer 3 POSTs idénticos,
    el 3° debe traer ok=False y respuesta con texto del límite."""
    client = chat_client_con_limit_2_por_minuto
    # Usamos session_id fijo para que la key del rate limiter sea estable;
    # mensajes distintos para evitar cache hits.
    base = {"tenant_slug": "demo", "session_id": "sess_rl_test"}

    r1 = client.post("/chat", json={**base, "message": "Pregunta uno"})
    assert r1.status_code == 200
    assert r1.json()["ok"] is True

    r2 = client.post("/chat", json={**base, "message": "Pregunta dos"})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True

    r3 = client.post("/chat", json={**base, "message": "Pregunta tres"})
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["ok"] is False
    assert "límite del demo" in body3["respuesta"]
    assert body3["session_id"] == "sess_rl_test"
    assert body3["provider_used"] is None
    assert body3["cached"] is False


def test_chat_endpoint_sin_limites_no_corta(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant SIN rate_limits → 10 requests pasan todas."""
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug, limits=None),
    )
    app = create_app()
    client = TestClient(app)

    for i in range(10):
        r = client.post(
            "/chat",
            json={
                "tenant_slug": "demo",
                "session_id": "sess_no_limit",
                "message": f"pregunta {i}",
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# E2E: /chat/stream
# ---------------------------------------------------------------------------


def test_chat_stream_endpoint_rate_limited(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 streams con per_minute=2, el 3° emite un solo `event: text`
    con texto del límite + `event: done`."""
    limits = RateLimitsConfig(per_minute=2)
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_streaming_bundle(slug, limits=limits),
    )
    app = create_app()
    client = TestClient(app)

    base = {"tenant_slug": "demo", "session_id": "sess_rl_stream"}

    # Primeras 2 pasan.
    for i, msg in enumerate(["uno", "dos"]):
        with client.stream(
            "POST", "/chat/stream", json={**base, "message": msg}
        ) as r:
            body = "".join(chunk for chunk in r.iter_text())
        events = _parse_sse(body)
        done = next(e for e in events if e[0] == "done")
        assert json.loads(done[1])["cached"] is False, f"stream {i + 1} cached"

    # La 3a debe cortar.
    with client.stream(
        "POST", "/chat/stream", json={**base, "message": "tres"}
    ) as r:
        body = "".join(chunk for chunk in r.iter_text())

    events = _parse_sse(body)
    text_events = [e for e in events if e[0] == "text"]
    done_events = [e for e in events if e[0] == "done"]
    assert len(text_events) == 1, (
        f"esperaba 1 evento text del límite, llegaron {len(text_events)}"
    )
    assert len(done_events) == 1
    text_data = json.loads(text_events[0][1])
    assert "límite del demo" in text_data["content"]
    done_data = json.loads(done_events[0][1])
    assert done_data["cached"] is False
    assert done_data["provider_used"] is None


# ---------------------------------------------------------------------------
# E2E: /telegram/webhook
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_demo_con_arnaldo_y_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> int:
    """Crea tenants/demo/ con allowed_chat_ids + rate_limits per_minute=2."""
    chat_id = 111_222_333
    root = tmp_path / "tenants"
    demo = root / "demo"
    demo.mkdir(parents=True)
    (demo / "system_prompt.md").write_text("test", encoding="utf-8")
    (demo / "tools.yaml").write_text("enabled: []\n", encoding="utf-8")
    (demo / "data_sources.yaml").write_text(
        "rate_limits:\n  per_minute: 2\n",
        encoding="utf-8",
    )
    (demo / "telegram_users.yaml").write_text(
        f"allowed_chat_ids: [{chat_id}]\n", encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))
    return chat_id


def test_telegram_webhook_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
    tenant_demo_con_arnaldo_y_rate_limit: int,
) -> None:
    """3 updates con per_minute=2 — en el 3° `send_message` se llama
    con el mensaje del límite (no con respuesta del agent)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    # Importante: el bundle que devuelve _get_or_build_bundle debe traer
    # las rate_limits porque _check_rate_limit las lee de ahí. Usamos
    # limits=2 idénticos a los del yaml para coherencia, pero el yaml
    # es la fuente real — si no parcheamos, el loader real lo leería
    # OK también.
    limits = RateLimitsConfig(per_minute=2)
    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug, limits=limits),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    chat_id = tenant_demo_con_arnaldo_y_rate_limit

    # Primeras 2: agente responde normal.
    for i in range(2):
        r = client.post(
            "/telegram/webhook",
            json=_valid_update(chat_id=chat_id, text=f"pregunta {i}"),
        )
        assert r.status_code == 200

    # 3a: debería ser el mensaje de límite.
    r = client.post(
        "/telegram/webhook",
        json=_valid_update(chat_id=chat_id, text="tercera"),
    )
    assert r.status_code == 200
    assert len(recorder.calls) == 3
    third = recorder.calls[-1]
    assert third["chat_id"] == chat_id
    assert "límite del demo" in third["text"]
    # Friendly prefix con reloj de arena.
    assert third["text"].startswith("⏳")
    # Y NO se envió la respuesta del agent.
    assert third["text"] != "Tenés 3 leads calientes."


def test_telegram_webhook_sin_limites_no_corta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tenant sin block rate_limits → 5 updates pasan todos."""
    chat_id = 555_666_777
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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    monkeypatch.setattr(
        telegram_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug, limits=None),
    )
    recorder = _SendMessageRecorder()
    monkeypatch.setattr(telegram_route, "send_message", recorder)

    app = create_app()
    client = TestClient(app)

    for i in range(5):
        r = client.post(
            "/telegram/webhook",
            json=_valid_update(chat_id=chat_id, text=f"pregunta {i}"),
        )
        assert r.status_code == 200

    # 5 respuestas, todas del agent (no rate-limit messages).
    assert len(recorder.calls) == 5
    for call in recorder.calls:
        assert call["text"] == "Tenés 3 leads calientes."
