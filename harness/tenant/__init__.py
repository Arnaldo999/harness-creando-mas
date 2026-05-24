"""Sistema multi-tenant: carga y aislamiento por cliente.

Cada tenant vive en `tenants/<slug>/` con tres archivos:
- `system_prompt.md` — prompt del rol (string libre).
- `tools.yaml` — qué tools enabled/disabled.
- `data_sources.yaml` — credenciales (vía env vars) y modelos LLM.
"""

from harness.tenant.auth import resolve_tenant
from harness.tenant.config import LLMConfig, PostgresSource, TenantConfig, TenantNotFoundError
from harness.tenant.loader import (
    TenantBundle,
    available_tenants,
    build_tenant_bundle,
    load_tenant_config,
)

__all__ = [
    "TenantConfig",
    "TenantNotFoundError",
    "PostgresSource",
    "LLMConfig",
    "TenantBundle",
    "load_tenant_config",
    "build_tenant_bundle",
    "available_tenants",
    "resolve_tenant",
]
