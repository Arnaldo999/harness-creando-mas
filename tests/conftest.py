"""Configuración pytest compartida."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asegura que los tests NO heredan env vars locales que rompan
    fixturas (ej. LOVBOT_AGENTE_API_KEY del .env real).

    También noopea `load_dotenv` dentro de `harness.api.app` para evitar
    que `create_app()` recargue el `.env` y pise la limpieza de env vars
    (bug encontrado durante validación local Fase 0)."""
    for k in [
        "LOVBOT_AGENTE_API_KEY",
        "LOVBOT_OPENAI_API_KEY",
        "LOVBOT_GEMINI_API_KEY",
        "TAVILY_API_KEY",
        "LOVBOT_PG_HOST",
        "LOVBOT_PG_PORT",
        "LOVBOT_PG_USER",
        "LOVBOT_PG_PASS",
    ]:
        monkeypatch.delenv(k, raising=False)

    # Evitar que create_app() re-lea el .env real durante tests.
    monkeypatch.setattr(
        "harness.api.app.load_dotenv",
        lambda *args, **kwargs: None,
        raising=False,
    )


@pytest.fixture
def tenants_root_real(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apunta HARNESS_TENANTS_ROOT al directorio `tenants/` del repo."""
    root = Path(__file__).resolve().parent.parent / "tenants"
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))
    return root


@pytest.fixture
def tenants_root_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Crea un tenants/ vacío en tmp para tests aislados."""
    root = tmp_path / "tenants"
    root.mkdir()
    monkeypatch.setenv("HARNESS_TENANTS_ROOT", str(root))
    return root
