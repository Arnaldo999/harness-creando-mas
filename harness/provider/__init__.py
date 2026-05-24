"""Providers de LLM.

- `base.Provider` — interfaz async común.
- `openai_provider.OpenAIProvider` — primario.
- `gemini_provider.GeminiProvider` — fallback.
- `router.ProviderRouter` — failover automático con logging.
- `mock.MockProvider` — para tests.
"""

from harness.provider.base import Provider
from harness.provider.gemini_provider import GeminiProvider
from harness.provider.mock import MockProvider
from harness.provider.openai_provider import OpenAIProvider
from harness.provider.router import ProviderRouter

__all__ = [
    "Provider",
    "OpenAIProvider",
    "GeminiProvider",
    "ProviderRouter",
    "MockProvider",
]
