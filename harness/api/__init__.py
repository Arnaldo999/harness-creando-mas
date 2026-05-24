"""Subpaquete `api` — incluye tipos genéricos del harness Y la app FastAPI.

- `harness.api.types` → tipos compartidos (Message, Block, Response, etc.)
- `harness.api.app`   → factory `create_app()` de FastAPI
- `harness.api.schemas` → contratos HTTP Pydantic
- `harness.api.routes.*` → routers FastAPI

Nota: aunque la carpeta se llama `api` por consistencia con `byo-harness`,
acá agrupa tanto los tipos LLM-agnósticos como el layer HTTP.
"""

from harness.api.types import (
    Block,
    BlockType,
    Message,
    Response,
    Role,
    StopReason,
    StreamEvent,
    StreamEventType,
    ToolDef,
    Usage,
)

__all__ = [
    "Block",
    "BlockType",
    "Message",
    "Response",
    "Role",
    "StopReason",
    "StreamEvent",
    "StreamEventType",
    "ToolDef",
    "Usage",
]
