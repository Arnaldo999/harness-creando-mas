"""Loader de tenants.

Lee `tenants/<slug>/{system_prompt.md, tools.yaml, data_sources.yaml}`,
resuelve env vars referenciadas por nombre, y arma un `TenantConfig`.

También expone `build_tenant_bundle()` que toma el TenantConfig y
construye el set listo-para-usar: provider router + tool registry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from harness.limits import RateLimitsConfig
from harness.provider import GeminiProvider, OpenAIProvider, Provider, ProviderRouter
from harness.tenant.config import (
    LLMConfig,
    PostgresSource,
    TenantConfig,
    TenantNotFoundError,
)
from harness.tool.ecosystem import (
    GenerarResumenConversacionTool,
    LookupLeadTool,
    QueryPostgresTool,
    TavilySearchTool,
    UpdateLeadEstadoTool,
)
from harness.tool.ecosystem.postgres import PostgresConnInfo
from harness.tool.registry import Registry

log = logging.getLogger(__name__)


# Path raíz donde viven los tenants. Override por env var para tests.
def _tenants_root() -> Path:
    custom = os.environ.get("HARNESS_TENANTS_ROOT")
    if custom:
        return Path(custom)
    # Por default, `tenants/` al lado del paquete `harness/`.
    return Path(__file__).resolve().parent.parent.parent / "tenants"


def available_tenants() -> list[str]:
    """Lista los slugs con directorio bajo `tenants/`."""
    root = _tenants_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def _resolve_env(name: str | None) -> str | None:
    if not name:
        return None
    val = os.environ.get(name)
    if not val:
        log.warning("tenant_env_missing", extra={"env_var": name})
    return val


def _coerce_positive_int(value: object, slug: str, field_name: str) -> int | None:
    """Convierte a int positivo. None/ausente → None (sin límite en
    esa ventana). Si vino algo raro (string, negativo, cero), logueamos
    y tratamos como None — preferimos arrancar sin esa ventana antes
    que crashear el tenant entero por un yaml mal escrito.
    """
    if value is None:
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        log.warning(
            "rate_limit_invalido",
            extra={"tenant": slug, "field": field_name, "valor": repr(value)},
        )
        return None
    if ivalue <= 0:
        log.warning(
            "rate_limit_no_positivo",
            extra={"tenant": slug, "field": field_name, "valor": ivalue},
        )
        return None
    return ivalue


def load_tenant_config(slug: str) -> TenantConfig:
    """Carga `tenants/<slug>/` y resuelve env vars referenciadas.

    Lanza `TenantNotFoundError` si el dir no existe.
    """
    root = _tenants_root() / slug
    if not root.is_dir():
        raise TenantNotFoundError(f"tenant '{slug}' no existe (esperado en {root})")

    # system_prompt.md (requerido).
    sp_path = root / "system_prompt.md"
    if not sp_path.is_file():
        raise FileNotFoundError(f"falta system_prompt.md en {root}")
    system_prompt = sp_path.read_text(encoding="utf-8").strip()

    # tools.yaml (requerido).
    tools_path = root / "tools.yaml"
    tools_enabled: list[str] = []
    tools_disabled: list[str] = []
    if tools_path.is_file():
        tdata = yaml.safe_load(tools_path.read_text(encoding="utf-8")) or {}
        tools_enabled = list(tdata.get("enabled") or [])
        tools_disabled = list(tdata.get("disabled") or [])

    # data_sources.yaml (requerido).
    ds_path = root / "data_sources.yaml"
    pg: PostgresSource | None = None
    tavily_key: str | None = None
    llm_primary: LLMConfig | None = None
    llm_fallback: LLMConfig | None = None

    rate_limits: RateLimitsConfig | None = None

    if ds_path.is_file():
        ds = yaml.safe_load(ds_path.read_text(encoding="utf-8")) or {}

        # Postgres.
        pg_block = ds.get("postgres")
        if pg_block:
            host = _resolve_env(pg_block.get("host_env")) or pg_block.get("host", "")
            port_raw = (
                _resolve_env(pg_block.get("port_env"))
                or str(pg_block.get("port", "5432"))
            )
            user = _resolve_env(pg_block.get("user_env")) or pg_block.get("user", "")
            pwd = _resolve_env(pg_block.get("password_env")) or pg_block.get("password", "")
            db = pg_block.get("database", "")
            try:
                port_int = int(port_raw)
            except ValueError:
                port_int = 5432
            pg = PostgresSource(
                host=host, port=port_int, user=user, password=pwd, database=db
            )

        # Tavily.
        tv_block = ds.get("tavily")
        if tv_block:
            tavily_key = _resolve_env(tv_block.get("api_key_env"))

        # LLM.
        llm_block = ds.get("llm") or {}
        if llm_block.get("primary"):
            p = llm_block["primary"]
            llm_primary = LLMConfig(
                provider=p.get("provider", "openai"),
                model=p.get("model", "gpt-5"),
                api_key=_resolve_env(p.get("api_key_env")),
                base_url=p.get("base_url"),
            )
        if llm_block.get("fallback"):
            f = llm_block["fallback"]
            llm_fallback = LLMConfig(
                provider=f.get("provider", "gemini"),
                model=f.get("model", "gemini-2.5-pro"),
                api_key=_resolve_env(f.get("api_key_env")),
                base_url=f.get("base_url"),
            )

        # Rate limits (opcional). Si el block no existe → tenant sin
        # límite, comportamiento legacy.
        rl_block = ds.get("rate_limits")
        if rl_block:
            rate_limits = RateLimitsConfig(
                per_minute=_coerce_positive_int(rl_block.get("per_minute"), slug, "per_minute"),
                per_hour=_coerce_positive_int(rl_block.get("per_hour"), slug, "per_hour"),
                per_day=_coerce_positive_int(rl_block.get("per_day"), slug, "per_day"),
            )

    # telegram_users.yaml (opcional). Solo leemos `allowed_chat_ids`.
    tg_path = root / "telegram_users.yaml"
    telegram_ids: list[int] = []
    if tg_path.is_file():
        try:
            tg_data = yaml.safe_load(tg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.warning(
                "telegram_users_yaml_invalido",
                extra={"tenant": slug, "err": str(e)},
            )
            tg_data = {}
        for item in tg_data.get("allowed_chat_ids") or []:
            try:
                telegram_ids.append(int(item))
            except (TypeError, ValueError):
                log.warning(
                    "telegram_allowed_id_no_entero",
                    extra={"tenant": slug, "valor": repr(item)},
                )

    return TenantConfig(
        slug=slug,
        system_prompt=system_prompt,
        tools_enabled=tools_enabled,
        tools_disabled=tools_disabled,
        postgres=pg,
        tavily_api_key=tavily_key,
        llm_primary=llm_primary,
        llm_fallback=llm_fallback,
        telegram_allowed_chat_ids=telegram_ids,
        rate_limits=rate_limits,
    )


@dataclass
class TenantBundle:
    """Conjunto listo-para-usar de un tenant: config + provider + registry."""

    config: TenantConfig
    provider: Provider
    tools: Registry


def _build_provider_from_config(llm: LLMConfig | None, system: str) -> Provider | None:
    if llm is None:
        return None
    if not llm.api_key:
        # Sin api_key resuelto (env var faltante o vacía) → skip silencioso.
        # El _resolve_env ya logueó el warning. Para el fallback esto es OK;
        # para el primary lo agarra build_tenant_bundle con su check.
        log.warning(
            "provider_skipped_missing_key",
            extra={"provider": llm.provider, "model": llm.model},
        )
        return None
    if llm.provider == "openai":
        return OpenAIProvider(
            api_key=llm.api_key,
            model=llm.model,
            system=system,
            base_url=llm.base_url,  # None = default (api.openai.com); seteado = endpoint OpenAI-compat (DeepSeek, Groq, etc.)
        )
    if llm.provider == "gemini":
        return GeminiProvider(
            api_key=llm.api_key,
            model=llm.model,
            system=system,
        )
    raise ValueError(f"provider desconocido: {llm.provider}")


def build_tenant_bundle(
    config: TenantConfig,
    *,
    override_provider: Provider | None = None,
) -> TenantBundle:
    """Materializa un TenantConfig en provider + tool registry listos.

    `override_provider` permite a los tests inyectar un MockProvider
    sin tocar env vars.
    """
    # Provider.
    if override_provider is not None:
        provider: Provider = override_provider
    else:
        primary = _build_provider_from_config(config.llm_primary, config.system_prompt)
        fallback = _build_provider_from_config(config.llm_fallback, config.system_prompt)
        if primary is None:
            raise ValueError(f"tenant {config.slug}: llm.primary no configurado")
        provider = ProviderRouter(primary=primary, fallback=fallback)

    # Tool registry — solo las enabled.
    registry = Registry()
    enabled = set(config.tools_enabled)

    if "query_postgres" in enabled and config.postgres is not None:
        registry.register(
            QueryPostgresTool(
                conn_info=_to_conn_info(config.postgres),
                tenant_slug=config.slug,
            )
        )
    if "lookup_lead" in enabled and config.postgres is not None:
        registry.register(
            LookupLeadTool(
                conn_info=_to_conn_info(config.postgres),
                tenant_slug=config.slug,
            )
        )
    if "update_lead_estado" in enabled and config.postgres is not None:
        registry.register(
            UpdateLeadEstadoTool(
                conn_info=_to_conn_info(config.postgres),
                tenant_slug=config.slug,
            )
        )
    if "generar_resumen_conversacion" in enabled and config.postgres is not None:
        registry.register(
            GenerarResumenConversacionTool(
                conn_info=_to_conn_info(config.postgres),
                tenant_slug=config.slug,
            )
        )
    if "tavily_search" in enabled:
        registry.register(TavilySearchTool(api_key=config.tavily_api_key))

    return TenantBundle(config=config, provider=provider, tools=registry)


def _to_conn_info(pg: PostgresSource) -> PostgresConnInfo:
    return PostgresConnInfo(
        host=pg.host,
        port=pg.port,
        user=pg.user,
        password=pg.password,
        database=pg.database,
    )
