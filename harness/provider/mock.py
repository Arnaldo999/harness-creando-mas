"""MockProvider — devuelve respuestas predefinidas (async).

Para tests del bucle del agente, del router de providers y de los
endpoints HTTP. Sin red, sin API key, sin gasto.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from harness.api import Message, Response, StopReason, StreamEvent, ToolDef
from harness.provider.base import Provider


class MockProvider(Provider):
    def __init__(
        self,
        responses: list[Response] | None = None,
        *,
        repeat_last: bool = False,
        model_name: str = "mock",
        provider_name: str = "mock",
        error: Exception | None = None,
        stream_events: list[list[StreamEvent]] | None = None,
    ) -> None:
        self._responses = list(responses) if responses else []
        self._repeat_last = repeat_last
        self._model = model_name
        self._provider_name = provider_name
        self._error = error
        self._system = ""
        self._sent: list[list[Message]] = []
        self._tools_history: list[list[ToolDef]] = []
        self._calls = 0
        # Si stream_events está seteado, send_stream usa esos eventos
        # directamente (no pasa por send()). Útil para tests del endpoint
        # SSE que necesitan controlar el formato exacto.
        self._stream_events = (
            [list(evs) for evs in stream_events] if stream_events else None
        )
        self._stream_calls = 0

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return self._system

    @system.setter
    def system(self, value: str) -> None:
        self._system = value

    async def send(self, messages: list[Message], tools: list[ToolDef]) -> Response:
        if self._error is not None:
            self._calls += 1
            raise self._error

        self._sent.append(list(messages))
        self._tools_history.append(list(tools))

        if self._calls < len(self._responses):
            r = self._responses[self._calls]
        elif self._repeat_last and self._responses:
            r = self._responses[-1]
        else:
            r = Response(stop_reason=StopReason.END_TURN)
        self._calls += 1
        return r

    async def send_stream(
        self, messages: list[Message], tools: list[ToolDef]
    ) -> AsyncIterator[StreamEvent]:
        # Si nos seteraron stream_events, los devolvemos sin pasar por send().
        if self._stream_events is not None:
            if self._error is not None:
                self._stream_calls += 1
                raise self._error
            self._sent.append(list(messages))
            self._tools_history.append(list(tools))
            idx = min(self._stream_calls, len(self._stream_events) - 1)
            events = self._stream_events[idx]
            self._stream_calls += 1
            for ev in events:
                yield ev
            return
        # Sin stream_events explícitos → delegamos al default del base
        # que llama a send() y emite eventos derivados.
        async for ev in super().send_stream(messages, tools):
            yield ev

    # ----- Introspección para tests -----

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def last_sent(self) -> list[Message] | None:
        return self._sent[-1] if self._sent else None
