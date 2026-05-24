"""Interfaz `Provider` (async).

Cualquier backend de LLM implementa esta interfaz. El bucle del agente
solo habla con providers a través de ella — cambiá la implementación
para cambiar de modelo o SDK.

Diferencia con el byo-harness: este `send` es async porque el harness
HTTP corre dentro de FastAPI/uvicorn y queremos no bloquear el event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from harness.api import Message, Response, StopReason, StreamEvent, ToolDef


class Provider(ABC):
    @abstractmethod
    async def send(self, messages: list[Message], tools: list[ToolDef]) -> Response:
        """Hace el viaje al modelo y devuelve la Response normalizada."""
        ...

    async def send_stream(
        self, messages: list[Message], tools: list[ToolDef]
    ) -> AsyncIterator[StreamEvent]:
        """Variante streaming. Por default delega a `send` y emite el
        resultado entero como una secuencia de eventos `text` /
        `tool_use_start` + un `stop` final.

        Los providers que sí soportan streaming nativo (OpenAI/Gemini)
        sobreescriben este método para emitir deltas reales del modelo.
        """
        from harness.api import BlockType  # noqa: PLC0415 — evita ciclo

        resp = await self.send(messages, tools)
        for block in resp.content:
            if block.type == BlockType.TEXT and block.text:
                yield StreamEvent(type="text", text=block.text)
            elif block.type == BlockType.TOOL_USE:
                yield StreamEvent(
                    type="tool_use_start",
                    tool_name=block.tool_name,
                    tool_use_id=block.tool_use_id,
                    tool_input=block.tool_input,
                )
        yield StreamEvent(
            type="stop",
            stop_reason=resp.stop_reason or StopReason.OTHER,
            usage=resp.usage,
        )

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre legible del provider — usado por el router en logs y
        en `ChatResponse.provider_used` ('openai' / 'gemini' / 'mock').
        """
        ...

    @property
    @abstractmethod
    def model(self) -> str:
        """Modelo actual."""
        ...

    @property
    @abstractmethod
    def system(self) -> str:
        """System prompt actual."""
        ...

    @system.setter
    @abstractmethod
    def system(self, value: str) -> None: ...
