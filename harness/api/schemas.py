"""Contratos HTTP Pydantic.

`ChatRequest` y `ChatResponse` son el formato del endpoint /chat.
Deliberadamente compatible con el body del webhook n8n actual
(`POST n8n.lovbot.ai/webhook/crm-ia-chat`) para que el switch en
`crm-v2.html` sea un solo cambio de URL.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """Body del POST /chat. Compatible con el webhook n8n."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(..., description="Texto del operador del CRM.")
    tenant_slug: str = Field(default="demo", description="Slug del tenant a usar.")
    session_id: str | None = Field(
        default=None,
        description="ID de sesión para continuar una conversación. Si es null, se crea una nueva.",
    )


class ChatResponse(BaseModel):
    """Response del POST /chat."""

    respuesta: str
    ok: bool = True
    tokens_in: int | None = None
    tokens_out: int | None = None
    provider_used: str | None = None  # "openai" | "gemini" | "mock"
    session_id: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "dev"
    tenants_loaded: list[str] = Field(default_factory=list)
