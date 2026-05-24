"""Registry de tools + interfaz `Tool` — copia adaptada del byo-harness.

El bucle del agente solo conoce el Registry — pide `definitions()` para
mandar al modelo y `execute(name, input)` para despachar. No sabe qué
herramientas existen ni cómo se implementan. Ese es todo el punto.

A diferencia del byo-harness, no usamos `default_registry` global.
Cada tenant tiene SU propio Registry construido por `harness.tenant.loader`,
porque la lista de tools enabled cambia por cliente.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness.api import ToolDef


class Tool(ABC):
    """Cada herramienta implementa dos métodos:

    - `definition()`: el schema que ve el modelo (ToolDef).
    - `execute(raw_input)`: la acción real cuando el modelo la pide.

    Por contrato, NUNCA crashea: errores se devuelven como string +
    flag para que el modelo los lea y pueda recuperarse.
    """

    @abstractmethod
    def definition(self) -> ToolDef: ...

    @abstractmethod
    async def execute(self, raw_input: str) -> tuple[str, bool]:
        """Devuelve (resultado_como_string, es_error).

        Las tools del harness HTTP son ASYNC porque la mayoría hace I/O
        (Postgres, HTTP, etc.). Si tu tool es CPU-bound, marcala async
        igual y resolvé sync por dentro.
        """
        ...


class Registry:
    """Mapa nombre → Tool. El agente habla solo con esto."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Agrega una tool. Si ya existe una con el mismo nombre, la sobreescribe."""
        self._tools[tool.definition().name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDef]:
        """Todas las definiciones, ORDENADAS por nombre.

        El orden determinístico importa para prompt caching: dos llamadas
        con las "mismas" tools que serialicen distinto invalidan el cache.
        """
        return [self._tools[n].definition() for n in sorted(self._tools)]

    async def execute(self, name: str, raw_input: str) -> tuple[str, bool]:
        """Despacha una tool call por nombre. Tools desconocidas devuelven
        un tool_result con error en vez de crashear — el modelo lo lee y
        puede auto-corregirse.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True
        return await tool.execute(raw_input)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
