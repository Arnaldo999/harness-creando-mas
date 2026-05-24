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
