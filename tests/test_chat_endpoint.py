"""Smoke test del endpoint /chat con FastAPI TestClient + MockProvider.

Stubea `build_tenant_bundle` para no instanciar OpenAI/Postgres reales.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api import Block, BlockType, Response, StopReason
from harness.api.app import create_app
from harness.api.routes import chat as chat_route
from harness.provider import MockProvider
from harness.tenant import TenantBundle
from harness.tenant.config import TenantConfig
from harness.tool.registry import Registry


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
        config=TenantConfig(
            slug=slug,
            system_prompt="test",
            tools_enabled=[],
        ),
        provider=provider,
        tools=Registry(),
    )


@pytest.fixture
def app_with_mock(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """App montada con bundles pre-cargados (no toca env vars)."""
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    app = create_app()
    return TestClient(app)


def test_health_responde(app_with_mock: TestClient) -> None:
    r = app_with_mock.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "demo" in body["tenants_loaded"]


def test_chat_basico_sin_auth(app_with_mock: TestClient) -> None:
    # Sin env var de auth, todo pasa.
    r = app_with_mock.post(
        "/chat",
        json={"message": "Hola, ¿cuántos leads calientes tengo?", "tenant_slug": "demo"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["respuesta"] == "Tenés 3 leads calientes."
    assert body["provider_used"] == "openai"
    assert body["session_id"].startswith("sess_")


def test_chat_genera_session_id_si_no_viene(app_with_mock: TestClient) -> None:
    r = app_with_mock.post(
        "/chat", json={"message": "hola", "tenant_slug": "demo"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"]


def test_chat_tenant_inexistente_404(app_with_mock: TestClient) -> None:
    r = app_with_mock.post(
        "/chat", json={"message": "hola", "tenant_slug": "no-existe"}
    )
    assert r.status_code == 404


def test_chat_con_bearer_requerido_y_ausente_da_401(
    tenants_root_real: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOVBOT_AGENTE_API_KEY", "supersecret")
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    app = create_app()
    client = TestClient(app)

    r = client.post(
        "/chat", json={"message": "hola", "tenant_slug": "demo"}
    )
    assert r.status_code == 401


def test_chat_con_bearer_correcto_pasa(
    tenants_root_real: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOVBOT_AGENTE_API_KEY", "supersecret")
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    app = create_app()
    client = TestClient(app)

    r = client.post(
        "/chat",
        json={"message": "hola", "tenant_slug": "demo"},
        headers={"Authorization": "Bearer supersecret"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_chat_con_bearer_incorrecto_da_401(
    tenants_root_real: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOVBOT_AGENTE_API_KEY", "supersecret")
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_bundle(slug),
    )
    app = create_app()
    client = TestClient(app)

    r = client.post(
        "/chat",
        json={"message": "hola", "tenant_slug": "demo"},
        headers={"Authorization": "Bearer otro-token"},
    )
    assert r.status_code == 401


def test_chat_cached_devuelve_cached_true_en_segunda_request(
    app_with_mock: TestClient,
) -> None:
    """Dos requests idénticos consecutivos: el segundo debe llegar
    desde el cache (cached=true, latency baja, mismo respuesta)."""
    import time

    payload = {"message": "Pregunta repetida idempotente", "tenant_slug": "demo"}

    r1 = app_with_mock.post("/chat", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["cached"] is False
    assert body1["respuesta"] == "Tenés 3 leads calientes."

    t0 = time.perf_counter()
    r2 = app_with_mock.post("/chat", json=payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["cached"] is True
    assert body2["respuesta"] == body1["respuesta"]
    # El cache hit debe ser instantáneo (sin pegada al MockProvider).
    # Tope generoso para CI lentos.
    assert elapsed_ms < 100


def test_chat_mensajes_distintos_no_comparten_cache(app_with_mock: TestClient) -> None:
    r1 = app_with_mock.post(
        "/chat", json={"message": "primera", "tenant_slug": "demo"}
    )
    r2 = app_with_mock.post(
        "/chat", json={"message": "segunda distinta", "tenant_slug": "demo"}
    )
    assert r1.json()["cached"] is False
    assert r2.json()["cached"] is False
