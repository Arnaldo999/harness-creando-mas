"""GeminiProvider — implementa `Provider` async sobre Google `google-genai`.

Usa el SDK moderno `google-genai` (NO el legacy `google-generativeai`).

Traducción clave del formato genérico → Gemini:
- Roles: USER → 'user', ASSISTANT → 'model'.
- Mensajes son `Content` con `parts`. Un part puede ser `text`,
  `function_call` o `function_response`.
- Tool calling: OpenAI usa `tool_calls[].function.{name,arguments}`;
  Gemini usa `function_call.{name,args}` (args = dict, no string).
  Nuestro provider normaliza al formato interno del harness.
- Las tools van fuera del historial, en `config.tools` como lista de
  `FunctionDeclaration`s.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from harness.api import (
    Block,
    BlockType,
    Message,
    Response,
    Role,
    StopReason,
    ToolDef,
    Usage,
)
from harness.provider.base import Provider


class GeminiProvider(Provider):
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "gemini-2.5-pro",
        system: str = "",
        max_tokens: int = 8192,
        timeout: float = 30.0,
    ) -> None:
        # Import perezoso para que el harness no rompa si google-genai no
        # está instalado en algún entorno mínimo (ej. tests del router con
        # mocks).
        from google import genai  # noqa: PLC0415

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._system = system
        self._max_tokens = max_tokens
        self._timeout = timeout

    # ----- Provider interface -----

    @property
    def name(self) -> str:
        return "gemini"

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
        from google.genai import types  # noqa: PLC0415

        contents = self._to_contents(messages)
        config_kwargs: dict[str, Any] = {
            "max_output_tokens": self._max_tokens,
        }
        if self._system:
            config_kwargs["system_instruction"] = self._system
        if tools:
            config_kwargs["tools"] = [
                types.Tool(function_declarations=self._to_function_declarations(tools))
            ]

        config = types.GenerateContentConfig(**config_kwargs)

        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )

        return self._from_gemini_response(resp)

    # ----- Traducción genérico → Gemini -----

    def _to_contents(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convierte nuestra lista de Message a la forma de Gemini.

        Gemini usa dicts {role, parts} donde role ∈ {'user', 'model'}.
        Cada part es un dict tipado: {text}, {function_call}, o
        {function_response}.
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            role = "user" if m.role == Role.USER else "model"
            parts: list[dict[str, Any]] = []
            for b in m.content:
                if b.type == BlockType.TEXT:
                    if b.text:
                        parts.append({"text": b.text})
                elif b.type == BlockType.TOOL_USE:
                    # Gemini quiere `args` como dict, no como string JSON.
                    try:
                        args = json.loads(b.tool_input) if b.tool_input else {}
                    except json.JSONDecodeError:
                        args = {"_raw": b.tool_input}
                    parts.append(
                        {
                            "function_call": {
                                "name": b.tool_name,
                                "args": args,
                            }
                        }
                    )
                elif b.type == BlockType.TOOL_RESULT:
                    content = (
                        f"[tool error] {b.tool_result}" if b.is_error else b.tool_result
                    )
                    # Gemini espera `response` como dict; metemos el string
                    # bajo una key para no perder estructura ni romper.
                    parts.append(
                        {
                            "function_response": {
                                "name": b.tool_use_id or "tool",
                                "response": {"content": content},
                            }
                        }
                    )
            if parts:
                out.append({"role": role, "parts": parts})
        return out

    def _to_function_declarations(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """ToolDef → FunctionDeclaration de Gemini.

        Schema es JSON Schema (subset). Mismo wrapper que OpenAI:
        {type:'object', properties:..., required:...}.
        """
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
                    "name": t.name,
                    "description": t.description,
                    "parameters": parameters,
                }
            )
        return out

    def _from_gemini_response(self, resp: Any) -> Response:
        """Normaliza la respuesta de Gemini al Response genérico del harness."""
        out = Response(stop_reason=StopReason.END_TURN)

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            out.stop_reason = StopReason.OTHER
            return out

        cand = candidates[0]
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", []) if content else []

        has_tool_call = False
        for part in parts:
            # Gemini parts: pueden tener text, function_call, function_response,
            # inline_data, etc. Nos interesan los dos primeros.
            text = getattr(part, "text", None)
            fcall = getattr(part, "function_call", None)
            if text:
                out.content.append(Block(type=BlockType.TEXT, text=text))
            if fcall:
                has_tool_call = True
                args = getattr(fcall, "args", {}) or {}
                # args puede venir como dict-like; serializamos a JSON
                # para mantener la interfaz interna (tool_input es string).
                try:
                    args_str = json.dumps(dict(args))
                except (TypeError, ValueError):
                    args_str = "{}"
                out.content.append(
                    Block(
                        type=BlockType.TOOL_USE,
                        tool_use_id=f"call_{uuid.uuid4().hex[:12]}",
                        tool_name=getattr(fcall, "name", "") or "",
                        tool_input=args_str,
                    )
                )

        if has_tool_call:
            out.stop_reason = StopReason.TOOL_USE
        else:
            finish = getattr(cand, "finish_reason", None)
            # finish_reason puede ser enum o string según versión del SDK.
            finish_str = (
                finish.name
                if hasattr(finish, "name")
                else (str(finish) if finish else "STOP")
            )
            if finish_str.upper() in {"STOP", "MAX_TOKENS"}:
                out.stop_reason = StopReason.END_TURN
            else:
                out.stop_reason = StopReason.OTHER

        usage = getattr(resp, "usage_metadata", None)
        if usage:
            out.usage = Usage(
                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                cache_read_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
            )

        return out
