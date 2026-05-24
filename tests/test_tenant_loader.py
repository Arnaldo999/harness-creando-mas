"""Tests del loader de tenants."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.tenant import available_tenants, load_tenant_config
from harness.tenant.config import TenantNotFoundError


def test_demo_tenant_existe(tenants_root_real: Path) -> None:
    assert "demo" in available_tenants()


def test_carga_demo_tenant(
    tenants_root_real: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOVBOT_PG_HOST", "pg.local")
    monkeypatch.setenv("LOVBOT_PG_PORT", "5432")
    monkeypatch.setenv("LOVBOT_PG_USER", "lovbot")
    monkeypatch.setenv("LOVBOT_PG_PASS", "secret")
    monkeypatch.setenv("LOVBOT_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LOVBOT_GEMINI_API_KEY", "gem-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test")

    cfg = load_tenant_config("demo")

    assert cfg.slug == "demo"
    assert "operador del CRM" in cfg.system_prompt
    assert "query_postgres" in cfg.tools_enabled
    assert "update_lead_estado" in cfg.tools_enabled
    assert "bash" in cfg.tools_disabled

    assert cfg.postgres is not None
    assert cfg.postgres.host == "pg.local"
    assert cfg.postgres.port == 5432
    assert cfg.postgres.database == "lovbot_crm_modelo"

    assert cfg.llm_primary is not None
    assert cfg.llm_primary.provider == "openai"
    # Switch a gpt-4o-mini (Fase 1) — sin reasoning overhead, más rápido
    # para tool_calls + streaming SSE. Ver tenants/demo/data_sources.yaml.
    assert cfg.llm_primary.model == "gpt-4o-mini"
    assert cfg.llm_primary.api_key == "sk-test"

    assert cfg.llm_fallback is not None
    assert cfg.llm_fallback.provider == "gemini"
    assert cfg.llm_fallback.model == "gemini-2.5-flash"

    assert cfg.tavily_api_key == "tv-test"


def test_tenant_inexistente_lanza_error(tenants_root_real: Path) -> None:
    with pytest.raises(TenantNotFoundError):
        load_tenant_config("no-existe")


def test_carga_sin_env_vars_no_crashea(tenants_root_real: Path) -> None:
    # Si no hay env vars, el loader igual debe armar TenantConfig (con
    # api_keys = None). Útil para que `available_tenants()` y la app
    # arranquen sin secretos.
    cfg = load_tenant_config("demo")
    assert cfg.slug == "demo"
    assert cfg.postgres is not None
    assert cfg.postgres.host == ""  # env no seteado → string vacío
    assert cfg.llm_primary is not None
    assert cfg.llm_primary.api_key is None
