"""Resolución de tenant y bearer auth.

Fase 0:
- Tenant = del body del request (campo `tenant_slug`), validando que el
  directorio exista.
- Auth = header `Authorization: Bearer <LOVBOT_AGENTE_API_KEY>`. Si el
  env var no está seteado, modo dev abierto (warn al boot).
"""

from __future__ import annotations

import logging
import os
import secrets

from harness.tenant.config import TenantNotFoundError
from harness.tenant.loader import available_tenants

log = logging.getLogger(__name__)


def get_required_bearer_token() -> str | None:
    """Devuelve el bearer esperado, o None si el server está en modo open."""
    return os.environ.get("LOVBOT_AGENTE_API_KEY")


def verify_bearer(provided: str | None) -> bool:
    """Compara el bearer del request contra el del env.

    Si el env no está seteado, todo pasa (modo dev). Si está seteado y
    no hay header / no matchea → False.
    """
    expected = get_required_bearer_token()
    if not expected:
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


def resolve_tenant(slug: str) -> str:
    """Valida que el slug pedido tenga directorio en `tenants/`.

    Devuelve el slug normalizado o lanza `TenantNotFoundError`.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        raise TenantNotFoundError("tenant_slug requerido")
    if slug not in available_tenants():
        raise TenantNotFoundError(
            f"tenant '{slug}' no existe. Disponibles: {available_tenants()}"
        )
    return slug
