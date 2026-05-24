"""Smoke test del endpoint /chat/stream con MockProvider streaming."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api import StopReason, StreamEvent, Usage
from harness.api.app import create_app
from harness.api.routes import chat as chat_route
from harness.provider import MockProvider
from harness.tenant import TenantBundle
from harness.tenant.config import TenantConfig
from harness.tool.registry import Registry


def _make_streaming_bundle(slug: str = "demo") -> TenantBundle:
    events = [
        [
            StreamEvent(type="text", text="Hola"),
            StreamEvent(type="text", text=" — tenés"),
            StreamEvent(type="text", text=" 3 leads."),
            StreamEvent(
                type="stop",
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=100, output_tokens=10),
            ),
        ]
    ]
    provider = MockProvider(
        provider_name="openai",
        stream_events=events,
    )
    return TenantBundle(
        config=TenantConfig(slug=slug, system_prompt="test", tools_enabled=[]),
        provider=provider,
        tools=Registry(),
    )


@pytest.fixture
def stream_client(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setattr(
        chat_route,
        "_get_or_build_bundle",
        lambda request, slug: _make_streaming_bundle(slug),
    )
    app = create_app()
    return TestClient(app)


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """Parser mínimo SSE: lista de (event_name, data_str)."""
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
    # Por las dudas, flush si quedó un evento sin newline final.
    if current_event is not None:
        events.append((current_event, "\n".join(current_data)))
    return events


def test_chat_stream_emite_texto_y_done(stream_client: TestClient) -> None:
    import json

    with stream_client.stream(
        "POST",
        "/chat/stream",
        json={"message": "hola", "tenant_slug": "demo"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in r.iter_text())

    events = _parse_sse(body)
    event_types = [e[0] for e in events]
    assert "text" in event_types
    assert "done" in event_types
    # Al menos 3 text events + 1 done.
    text_events = [e for e in events if e[0] == "text"]
    assert len(text_events) >= 3
    # Texto concatenado debe contener "leads".
    full = "".join(json.loads(d)["content"] for _, d in text_events)
    assert "leads" in full

    done_event = next(e for e in events if e[0] == "done")
    done_data = json.loads(done_event[1])
    assert done_data["cached"] is False
    assert done_data["session_id"].startswith("sess_")
    assert done_data["provider_used"] == "openai"


def test_chat_stream_segunda_request_es_cached(stream_client: TestClient) -> None:
    import json

    # Primer hit → llena cache.
    with stream_client.stream(
        "POST",
        "/chat/stream",
        json={"message": "consulta cacheable", "tenant_slug": "demo"},
    ) as r:
        for _ in r.iter_text():
            pass

    # Segunda con mismo mensaje → cached=True.
    with stream_client.stream(
        "POST",
        "/chat/stream",
        json={"message": "consulta cacheable", "tenant_slug": "demo"},
    ) as r:
        body = "".join(chunk for chunk in r.iter_text())

    events = _parse_sse(body)
    done = next(e for e in events if e[0] == "done")
    assert json.loads(done[1])["cached"] is True


def test_chat_stream_tenant_inexistente_404(stream_client: TestClient) -> None:
    r = stream_client.post(
        "/chat/stream",
        json={"message": "x", "tenant_slug": "no-existe"},
    )
    assert r.status_code == 404
