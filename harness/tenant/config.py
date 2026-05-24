"""Dataclasses de configuración por tenant."""

from __future__ import annotations

from dataclasses import dataclass, field


class TenantNotFoundError(LookupError):
    """No existe directorio `tenants/<slug>/`."""


@dataclass
class PostgresSource:
    """Conexión Postgres del tenant.

    Las credenciales reales se resuelven desde env vars referenciadas
    por nombre en `data_sources.yaml`. El loader hace la resolución.
    """

    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass
class LLMConfig:
    """Config de un provider LLM (primario o fallback)."""

    provider: str  # "openai" | "gemini"
    model: str
    api_key: str | None  # resuelto desde env var


@dataclass
class TenantConfig:
    """Snapshot inmutable de la configuración de un tenant.

    Construido por `harness.tenant.loader.load_tenant_config` desde los
    archivos en `tenants/<slug>/`.
    """

    slug: str
    system_prompt: str
    tools_enabled: list[str]
    tools_disabled: list[str] = field(default_factory=list)
    postgres: PostgresSource | None = None
    tavily_api_key: str | None = None
    llm_primary: LLMConfig | None = None
    llm_fallback: LLMConfig | None = None
