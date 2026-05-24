"""GET /health — para healthcheck de Coolify."""

from __future__ import annotations

import os

from fastapi import APIRouter

from harness.api.schemas import HealthResponse
from harness.tenant.loader import available_tenants

router = APIRouter()


def _detect_version() -> str:
    # Coolify inyecta SOURCE_COMMIT por default; si no, intentamos git local.
    return (
        os.environ.get("SOURCE_COMMIT")
        or os.environ.get("GIT_SHA")
        or os.environ.get("COMMIT_SHA")
        or "dev"
    )[:12]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=_detect_version(),
        tenants_loaded=available_tenants(),
    )
