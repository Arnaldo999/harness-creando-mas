"""OpenAIProvider — implementa `Provider` async sobre la API de OpenAI.

Es el ÚNICO archivo del harness que importa `openai`. Si encontrás tipos
`openai.*` en otro lado, hay una fuga.

Adaptado del byo-harness:
- `send` es async (usa `AsyncOpenAI`).
- Expone `name` para identificarse en el router.
- `model` es read-only en runtime (el TenantConfig lo fija al boot).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from harness.api import (
    Block,
    BlockType,
    Message,
    Response,
    Role,
    StopReason,
    StreamEvent,
    ToolDef,
    Usage,
)
from harness.provider.base import Provider


class OpenAIProvider(Provider):
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        system: str = "",
        max_tokens: int = 8192,
        timeout: float = 20.0,
        max_retries: int = 1,
        base_url: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        self._system = system
        self._max_tokens = max_tokens

    # ----- Provider interface -----

    @property
    def name(self) -> str:
        return "openai"

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
        # GPT-5 + reasoning models (o1, o3, gpt-5*) usan `max_completion_tokens`.
        # Modelos clásicos (gpt-4o, gpt-4-turbo, gpt-3.5) usan `max_tokens`.
        # `max_completion_tokens` también funciona con modelos clásicos en SDK
        # >= 1.40, así que lo usamos como default seguro.
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_messages(messages),
            "max_completion_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)
        resp = await self._client.chat.completions.create(**kwargs)

        if not resp.choices:
            return Response(stop_reason=StopReason.OTHER)
        choice = resp.choices[0]

        out = Response(stop_reason=_from_finish_reason(choice.finish_reason))
        if choice.message.content:
            out.content.append(Block(type=BlockType.TEXT, text=choice.message.content))
        for tc in choice.message.tool_calls or []:
            out.content.append(
                Block(
                    type=BlockType.TOOL_USE,
                    tool_use_id=tc.id,
                    tool_name=tc.function.name,
                    tool_input=tc.function.arguments,
                )
            )

        if resp.usage:
            cached = 0
            details = getattr(resp.usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
            out.usage = Usage(
                input_tokens=resp.usage.prompt_tokens or 0,
                output_tokens=resp.usage.completion_tokens or 0,
                cache_read_tokens=cached,
            )

        return out

    async def send_stream(
        self, messages: list[Message], tools: list[ToolDef]
    ) -> AsyncIterator[StreamEvent]:
        """Streaming nativo via `stream=True`.

        Acumula deltas de tool_calls (vienen fragmentados con `index`),
        emite text deltas inline, y al final yieldea un `stop` con
        finish_reason + usage (si el SDK los entrega via
        `stream_options={"include_usage": True}`).
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_messages(messages),
            "max_completion_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)

        # Acumulador de tool_calls. OpenAI manda los argumentos en deltas
        # con un `index` por tool_call; hay que pegarlos.
        tool_calls_acc: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        final_usage: Usage | None = None

        async for chunk in stream:
            # Usage llega en el último chunk cuando se pide include_usage.
            usage_obj = getattr(chunk, "usage", None)
            if usage_obj is not None:
                cached = 0
                details = getattr(usage_obj, "prompt_tokens_details", None)
                if details is not None:
                    cached = getattr(details, "cached_tokens", 0) or 0
                final_usage = Usage(
                    input_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
                    cache_read_tokens=cached,
                )

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]

            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            # Text delta.
            content_delta = getattr(delta, "content", None)
            if content_delta:
                yield StreamEvent(type="text", text=content_delta)

            # Tool call deltas.
            tcalls = getattr(delta, "tool_calls", None) or []
            for tc in tcalls:
                idx = getattr(tc, "index", 0) or 0
                slot = tool_calls_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        # Tool calls completos → emitir uno por uno.
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            if slot["name"]:
                yield StreamEvent(
                    type="tool_use_start",
                    tool_use_id=slot["id"],
                    tool_name=slot["name"],
                    tool_input=slot["arguments"] or "{}",
                )

        stop = _from_finish_reason(finish_reason)
        yield StreamEvent(
            type="stop",
            stop_reason=stop,
            usage=final_usage or Usage(),
        )

    # ----- Traducción genérico → OpenAI -----

    def _to_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Traduce nuestra lista de Message a la forma de OpenAI.

        - tool_results: cada uno como su propio mensaje con role='tool'.
        - asistente con texto + tool_use: UN solo mensaje con `content`
          y `tool_calls` ambos poblados.
        """
        out: list[dict[str, Any]] = []
        if self._system:
            out.append({"role": "system", "content": self._system})

        for m in messages:
            if m.role == Role.USER:
                text_parts: list[str] = []
                tool_results: list[dict[str, Any]] = []
                for b in m.content:
                    if b.type == BlockType.TEXT:
                        text_parts.append(b.text)
                    elif b.type == BlockType.TOOL_RESULT:
                        content = (
                            f"[tool error] {b.tool_result}" if b.is_error else b.tool_result
                        )
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": b.tool_use_id,
                                "content": content,
                            }
                        )
                if text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
                out.extend(tool_results)

            elif m.role == Role.ASSISTANT:
                text_parts = []
                tool_calls: list[dict[str, Any]] = []
                for b in m.content:
                    if b.type == BlockType.TEXT:
                        text_parts.append(b.text)
                    elif b.type == BlockType.TOOL_USE:
                        tool_calls.append(
                            {
                                "id": b.tool_use_id,
                                "type": "function",
                                "function": {
                                    "name": b.tool_name,
                                    "arguments": b.tool_input,
                                },
                            }
                        )
                msg: dict[str, Any] = {"role": "assistant"}
                msg["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)

        return out

    def _to_tools(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """ToolDef → formato OpenAI (sobre JSON Schema completo)."""
        out: list[dict[str, Any]] = []
        for t in tools:
            parameters: dict[str, Any] = {
                "type": "object",
                "properties": t.input_schema,
            }
            if t.required:
                parameters["required"] = t.required
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": parameters,
                    },
                }
            )
        return out


def _from_finish_reason(reason: str | None) -> StopReason:
    if reason == "stop":
        return StopReason.END_TURN
    if reason == "tool_calls":
        return StopReason.TOOL_USE
    return StopReason.OTHER
