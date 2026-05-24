"""Smoke runner local: levanta uvicorn con MockProvider y deja /chat/stream
sirviendo el tenant `demo` SIN pegarle a OpenAI/Gemini real.

NO commitear este uso en producción — es solo para validar la Fase 1
en un terminal con `curl -N ...`.

Uso:
    python scripts/_smoke_sse_local.py
    # otra terminal:
    curl -N -d '{"message":"hola","tenant_slug":"demo"}' \\
         -H 'Content-Type: application/json' \\
         http://localhost:8765/chat/stream
"""

from __future__ import annotations

import os

import uvicorn

# Limpiar env del .env real para que la app monte sin auth.
for _k in (
    "LOVBOT_AGENTE_API_KEY",
    "LOVBOT_OPENAI_API_KEY",
    "LOVBOT_GEMINI_API_KEY",
):
    os.environ.pop(_k, None)


def build() -> object:
    """Factory para uvicorn — monta create_app() y monkey-patchea
    el tenant builder con un MockProvider streaming."""
    # Import perezoso para que el unset de env aplique primero.
    import harness.api.app as app_mod

    app_mod.load_dotenv = lambda *a, **k: None

    from harness.api import StopReason, StreamEvent, Usage
    from harness.api.app import create_app
    from harness.api.routes import chat as chat_route
    from harness.provider import MockProvider
    from harness.tenant import TenantBundle
    from harness.tenant.config import TenantConfig
    from harness.tool.registry import Registry

    def _bundle(slug: str = "demo") -> TenantBundle:
        events = [
            [
                StreamEvent(type="text", text="Hola"),
                StreamEvent(type="text", text=" — tenés"),
                StreamEvent(type="text", text=" 12 leads calientes."),
                StreamEvent(
                    type="stop",
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(input_tokens=100, output_tokens=10),
                ),
            ]
        ]
        return TenantBundle(
            config=TenantConfig(slug=slug, system_prompt="test", tools_enabled=[]),
            provider=MockProvider(provider_name="openai", stream_events=events),
            tools=Registry(),
        )

    chat_route._get_or_build_bundle = lambda request, slug: _bundle(slug)
    return create_app()


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=8765, log_level="info")
