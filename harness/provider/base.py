"""Interfaz `Provider` (async).

Cualquier backend de LLM implementa esta interfaz. El bucle del agente
solo habla con providers a través de ella — cambiá la implementación
para cambiar de modelo o SDK.

Diferencia con el byo-harness: este `send` es async porque el harness
HTTP corre dentro de FastAPI/uvicorn y queremos no bloquear el event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness.api import Message, Response, ToolDef


class Provider(ABC):
    @abstractmethod
    async def send(self, messages: list[Message], tools: list[ToolDef]) -> Response:
        """Hace el viaje al modelo y devuelve la Response normalizada."""
        ...

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
