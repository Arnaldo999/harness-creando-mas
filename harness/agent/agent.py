"""Agent loop async — adaptación HTTP del byo-harness.

Encapsula UNA conversación: provider + tool registry + messages.

Diferencias vs. byo-harness CLI:
- Todo es async (FastAPI corre dentro del event loop).
- No hay `confirm` interactivo — auto-aprueba todas las tools (las
  tools "peligrosas" como bash/write_file no están en el set HTTP por
  defecto; ver tenants/<slug>/tools.yaml).
- No imprime nada — la respuesta final se devuelve como string.
- Sin spinner ni UI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from harness.api import (
    Block,
    BlockType,
    Message,
    Response,
    Role,
    StopReason,
    StreamEvent,
    Usage,
)
from harness.provider.base import Provider
from harness.tool.registry import Registry

log = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 20_000


def _truncate_tool_result(result: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(result) <= limit:
        return result
    truncated = len(result) - limit
    return (
        result[:limit]
        + f"\n\n[OUTPUT TRUNCATED — {truncated:,} more chars omitted. "
        f"Original was {len(result):,} chars. Narrowá tu query.]"
    )


class Agent:
    """Una conversación HTTP. Una instancia por request (los messages
    se hidratan desde el session store antes de `send`)."""

    def __init__(
        self,
        provider: Provider,
        tools: Registry,
        *,
        max_turns: int = 12,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.max_turns = max_turns
        self._messages: list[Message] = []
        self._usage_total = Usage()
        # Conjunto de tool names invocadas durante esta conversación.
        # Lo expone el caché para decidir si la respuesta es cacheable
        # (skip cache si se usó alguna tool de escritura).
        self._tool_names_used: set[str] = set()

    # ----- Estado -----

    @property
    def messages(self) -> list[Message]:
        return self._messages

    def set_messages(self, msgs: list[Message]) -> None:
        self._messages = list(msgs)

    @property
    def usage(self) -> Usage:
        return self._usage_total

    # ----- Bucle -----

    async def send(self, prompt: str) -> str:
        """Append `prompt` como user message y corre el bucle hasta que
        el modelo deje de pedir tools. Devuelve el texto final.
        """
        self._messages.append(
            Message(role=Role.USER, content=[Block(type=BlockType.TEXT, text=prompt)])
        )
        return await self._loop()

    async def _loop(self) -> str:
        final_text_parts: list[str] = []

        for turn in range(self.max_turns):
            resp: Response = await self.provider.send(self._messages, self.tools.definitions())

            # Acumular usage.
            self._usage_total = self._usage_total.add(resp.usage)

            # Append turno asistente al historial.
            self._messages.append(Message(role=Role.ASSISTANT, content=list(resp.content)))

            tool_results: list[Block] = []
            has_tool_call = False
            for block in resp.content:
                if block.type == BlockType.TEXT:
                    if block.text:
                        final_text_parts.append(block.text)
                elif block.type == BlockType.TOOL_USE:
                    has_tool_call = True
                    self._tool_names_used.add(block.tool_name)
                    log.info(
                        "tool_call",
                        extra={
                            "turn": turn,
                            "tool": block.tool_name,
                            "input_preview": (block.tool_input or "")[:200],
                        },
                    )
                    result, is_error = await self._execute_tool(
                        block.tool_name, block.tool_input
                    )
                    tool_results.append(
                        Block(
                            type=BlockType.TOOL_RESULT,
                            tool_use_id=block.tool_use_id,
                            tool_result=result,
                            is_error=is_error,
                        )
                    )

            if resp.stop_reason != StopReason.TOOL_USE or not has_tool_call:
                return "\n".join(p for p in final_text_parts if p).strip()

            self._messages.append(Message(role=Role.USER, content=tool_results))

        # Loop budget agotado.
        log.warning("agent_max_turns_reached", extra={"max_turns": self.max_turns})
        text = "\n".join(p for p in final_text_parts if p).strip()
        if not text:
            text = (
                "Disculpá, no pude completar la consulta en el tiempo disponible. "
                "Probá reformulando la pregunta de forma más específica."
            )
        return text

    async def _execute_tool(self, name: str, raw_input: str) -> tuple[str, bool]:
        result, is_error = await self.tools.execute(name, raw_input)
        if len(result) > MAX_TOOL_OUTPUT_CHARS:
            log.info(
                "tool_output_truncated",
                extra={"tool": name, "orig_len": len(result), "limit": MAX_TOOL_OUTPUT_CHARS},
            )
            result = _truncate_tool_result(result)
        return result, is_error

    # ----- Streaming -----

    @property
    def tool_names_used(self) -> list[str]:
        """Lista de tool names invocadas durante esta conversación.
        Usado por el caché para decidir si la respuesta es cacheable."""
        return list(self._tool_names_used)

    async def send_stream(self, prompt: str) -> AsyncIterator[StreamEvent]:
        """Variante streaming del agent loop.

        Emite eventos a medida que el modelo genera (text deltas) y
        avisa cuando entra/sale de tool calls. El estado interno
        (`messages`, `usage`, `tool_names_used`) queda igual que post-`send`
        — útil para que el endpoint persista la sesión al terminar.
        """
        self._messages.append(
            Message(role=Role.USER, content=[Block(type=BlockType.TEXT, text=prompt)])
        )

        for turn in range(self.max_turns):
            # Acumulamos lo que el provider stream emita para reconstruir
            # el turno assistant (necesario para persistir messages).
            assistant_text_parts: list[str] = []
            tool_uses: list[Block] = []
            stop_reason: StopReason | None = None
            usage_for_turn: Usage = Usage()

            async for ev in self.provider.send_stream(
                self._messages, self.tools.definitions()
            ):
                if ev.type == "text":
                    assistant_text_parts.append(ev.text)
                    yield ev  # text delta directo al cliente
                elif ev.type == "tool_use_start":
                    tool_uses.append(
                        Block(
                            type=BlockType.TOOL_USE,
                            tool_use_id=ev.tool_use_id,
                            tool_name=ev.tool_name,
                            tool_input=ev.tool_input,
                        )
                    )
                elif ev.type == "stop":
                    if ev.stop_reason is not None:
                        stop_reason = ev.stop_reason
                    if ev.usage is not None:
                        usage_for_turn = ev.usage
                # "usage" y "tool_use_complete" no vienen del provider
                # base; los emitimos nosotros al ejecutar tools.

            # Acumular usage del turno.
            self._usage_total = self._usage_total.add(usage_for_turn)

            # Persistir turno assistant.
            assistant_content: list[Block] = []
            joined_text = "".join(assistant_text_parts)
            if joined_text:
                assistant_content.append(Block(type=BlockType.TEXT, text=joined_text))
            assistant_content.extend(tool_uses)
            self._messages.append(Message(role=Role.ASSISTANT, content=assistant_content))

            # Si hay tools → ejecutar y continuar.
            if stop_reason == StopReason.TOOL_USE and tool_uses:
                tool_results: list[Block] = []
                for use in tool_uses:
                    self._tool_names_used.add(use.tool_name)
                    log.info(
                        "tool_call_stream",
                        extra={
                            "turn": turn,
                            "tool": use.tool_name,
                            "input_preview": (use.tool_input or "")[:200],
                        },
                    )
                    # Avisar al cliente que arrancó la tool.
                    yield StreamEvent(
                        type="tool_use_start",
                        tool_use_id=use.tool_use_id,
                        tool_name=use.tool_name,
                        tool_input=use.tool_input,
                    )
                    t0 = time.perf_counter()
                    result, is_error = await self._execute_tool(
                        use.tool_name, use.tool_input
                    )
                    ms = (time.perf_counter() - t0) * 1000
                    yield StreamEvent(
                        type="tool_use_complete",
                        tool_use_id=use.tool_use_id,
                        tool_name=use.tool_name,
                        tool_result=result,
                        is_error=is_error,
                        latency_ms=round(ms, 1),
                    )
                    tool_results.append(
                        Block(
                            type=BlockType.TOOL_RESULT,
                            tool_use_id=use.tool_use_id,
                            tool_result=result,
                            is_error=is_error,
                        )
                    )
                self._messages.append(Message(role=Role.USER, content=tool_results))
                continue

            # No hay tools → fin de la conversación.
            yield StreamEvent(
                type="stop",
                stop_reason=stop_reason or StopReason.END_TURN,
                usage=self._usage_total,
            )
            return

        # Loop budget agotado.
        log.warning("agent_max_turns_reached_stream", extra={"max_turns": self.max_turns})
        yield StreamEvent(
            type="stop",
            stop_reason=StopReason.OTHER,
            usage=self._usage_total,
        )
