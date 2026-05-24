"""Tipos agnósticos al proveedor.

Son la "intersección mínima" que cualquier API mayor de LLM necesita.
Los providers traducen entre estos tipos y los del SDK específico
(OpenAI, Gemini, etc.). Ningún otro módulo del harness importa SDKs
de proveedor — solo estos tipos.

Copia adaptada de `byo-harness-python/harness/api/types.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class BlockType(str, Enum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    OTHER = "other"


@dataclass
class Block:
    """Pedazo de contenido de un mensaje. Los campos se interpretan
    según `type`. Heredamos la forma de Anthropic (mensaje = lista de
    bloques heterogéneos) porque es la más expresiva; los providers
    tipo OpenAI/Gemini hacen el "explode" en su traducción interna.
    """

    type: BlockType

    # Para BlockType.TEXT
    text: str = ""

    # Para BlockType.TOOL_USE y BlockType.TOOL_RESULT
    tool_use_id: str = ""

    # Para BlockType.TOOL_USE
    tool_name: str = ""
    tool_input: str = ""  # JSON crudo, pass-through al provider

    # Para BlockType.TOOL_RESULT
    tool_result: str = ""
    is_error: bool = False


@dataclass
class Message:
    role: Role
    content: list[Block] = field(default_factory=list)

    def has_tool_result(self) -> bool:
        return any(b.type == BlockType.TOOL_RESULT for b in self.content)


@dataclass
class ToolDef:
    """Definición de una herramienta tal como la ve el modelo."""

    name: str
    description: str
    # input_schema contiene SOLO las properties (mapa name → schema).
    # El provider envuelve esto en {"type":"object","properties":...} al
    # traducir al formato nativo.
    input_schema: dict[str, object] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass
class Response:
    """Lo que devuelve `Provider.send`. Forma genérica, no del SDK."""

    content: list[Block] = field(default_factory=list)
    stop_reason: StopReason = StopReason.OTHER
    usage: Usage = field(default_factory=Usage)
