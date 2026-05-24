"""Tests del ProviderRouter — failover OpenAI → Gemini."""

from __future__ import annotations

import pytest

from harness.api import Block, BlockType, Response, StopReason
from harness.provider import MockProvider, ProviderRouter
from harness.provider.router import ProvidersExhaustedError, _should_failover


def _ok_response(text: str = "ok") -> Response:
    return Response(
        content=[Block(type=BlockType.TEXT, text=text)],
        stop_reason=StopReason.END_TURN,
    )


class _FakeRateLimit(Exception):
    """Imita openai.RateLimitError por nombre de clase."""


_FakeRateLimit.__name__ = "RateLimitError"


class _FakeInternalServerError(Exception):
    pass


_FakeInternalServerError.__name__ = "InternalServerError"


class _FakeRandomError(Exception):
    pass


class TestShouldFailover:
    def test_timeout_dispara_failover(self) -> None:
        assert _should_failover(TimeoutError("timed out")) is True

    def test_rate_limit_dispara_failover(self) -> None:
        assert _should_failover(_FakeRateLimit()) is True

    def test_internal_server_error_dispara_failover(self) -> None:
        assert _should_failover(_FakeInternalServerError()) is True

    def test_error_random_no_dispara_failover(self) -> None:
        assert _should_failover(_FakeRandomError("bug")) is False

    def test_status_code_5xx_dispara_failover(self) -> None:
        err = ValueError("boom")
        err.status_code = 503  # type: ignore[attr-defined]
        assert _should_failover(err) is True


class TestProviderRouter:
    @pytest.mark.asyncio
    async def test_primario_responde_no_usa_fallback(self) -> None:
        primary = MockProvider([_ok_response("primario")], provider_name="openai")
        fallback = MockProvider([_ok_response("fallback")], provider_name="gemini")
        router = ProviderRouter(primary=primary, fallback=fallback)

        resp = await router.send([], [])
        assert resp.content[0].text == "primario"
        assert router.last_used == "openai"
        assert fallback.calls == 0

    @pytest.mark.asyncio
    async def test_failover_a_gemini_cuando_openai_da_rate_limit(self) -> None:
        primary = MockProvider(
            provider_name="openai",
            error=_FakeRateLimit("rate limited"),
        )
        fallback = MockProvider([_ok_response("gemini-ok")], provider_name="gemini")
        router = ProviderRouter(primary=primary, fallback=fallback)

        resp = await router.send([], [])
        assert resp.content[0].text == "gemini-ok"
        assert router.last_used == "gemini"
        assert primary.calls == 1
        assert fallback.calls == 1

    @pytest.mark.asyncio
    async def test_failover_a_gemini_cuando_openai_da_timeout(self) -> None:
        primary = MockProvider(
            provider_name="openai", error=TimeoutError("timeout")
        )
        fallback = MockProvider([_ok_response("gemini-ok")], provider_name="gemini")
        router = ProviderRouter(primary=primary, fallback=fallback)

        resp = await router.send([], [])
        assert resp.content[0].text == "gemini-ok"
        assert router.last_used == "gemini"

    @pytest.mark.asyncio
    async def test_ambos_fallan_lanza_providers_exhausted(self) -> None:
        primary = MockProvider(
            provider_name="openai", error=_FakeRateLimit("rate limited")
        )
        fallback = MockProvider(
            provider_name="gemini", error=_FakeInternalServerError("down")
        )
        router = ProviderRouter(primary=primary, fallback=fallback)

        with pytest.raises(ProvidersExhaustedError):
            await router.send([], [])

    @pytest.mark.asyncio
    async def test_error_no_failover_se_propaga(self) -> None:
        primary = MockProvider(
            provider_name="openai", error=_FakeRandomError("bug en codigo")
        )
        fallback = MockProvider([_ok_response("gemini-ok")], provider_name="gemini")
        router = ProviderRouter(primary=primary, fallback=fallback)

        # _FakeRandomError no está en la whitelist de failover, así que
        # debe propagarse sin tocar el fallback.
        with pytest.raises(_FakeRandomError):
            await router.send([], [])
        assert fallback.calls == 0


class TestProviderRouterStreaming:
    @pytest.mark.asyncio
    async def test_send_stream_delega_al_primary(self) -> None:
        """El router debe delegar send_stream al primary si todo bien."""
        from harness.api import StopReason, StreamEvent, Usage

        primary = MockProvider(
            provider_name="openai",
            stream_events=[
                [
                    StreamEvent(type="text", text="hola"),
                    StreamEvent(type="text", text=" mundo"),
                    StreamEvent(
                        type="stop",
                        stop_reason=StopReason.END_TURN,
                        usage=Usage(input_tokens=5, output_tokens=2),
                    ),
                ]
            ],
        )
        fallback = MockProvider(
            provider_name="gemini",
            stream_events=[[StreamEvent(type="text", text="NO debería verse")]],
        )
        router = ProviderRouter(primary=primary, fallback=fallback)

        collected: list[StreamEvent] = []
        async for ev in router.send_stream([], []):
            collected.append(ev)

        text_events = [e for e in collected if e.type == "text"]
        assert [e.text for e in text_events] == ["hola", " mundo"]
        stop = next(e for e in collected if e.type == "stop")
        assert stop.stop_reason == StopReason.END_TURN
        assert router.last_used == "openai"

    @pytest.mark.asyncio
    async def test_send_stream_failover_a_fallback_si_primary_revienta_pre_chunk(
        self,
    ) -> None:
        """Si el primary tira RateLimitError antes del primer chunk →
        el router cae al fallback."""
        from harness.api import StopReason, StreamEvent, Usage

        primary = MockProvider(
            provider_name="openai",
            stream_events=[[]],  # ignored — error tira primero
            error=_FakeRateLimit("rate"),
        )
        fallback = MockProvider(
            provider_name="gemini",
            stream_events=[
                [
                    StreamEvent(type="text", text="gemini stream"),
                    StreamEvent(
                        type="stop",
                        stop_reason=StopReason.END_TURN,
                        usage=Usage(input_tokens=1, output_tokens=1),
                    ),
                ]
            ],
        )
        router = ProviderRouter(primary=primary, fallback=fallback)

        collected: list[StreamEvent] = []
        async for ev in router.send_stream([], []):
            collected.append(ev)

        text_events = [e for e in collected if e.type == "text"]
        assert any("gemini" in e.text for e in text_events)
        assert router.last_used == "gemini"
