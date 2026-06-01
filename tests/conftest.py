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


# -------------------------------------------------------------------------
# Postgres real efímero (Cap 14 del blueprint — integración SIN mocks)
# -------------------------------------------------------------------------
#
# En vez de mockear la base de datos, levantamos un Postgres real en Docker
# para la corrida de tests, lo llenamos, corremos los tests contra la DB real
# y lo tiramos. Es la pieza que el blueprint-harness/14_verificacion_testing.md
# describía como TARGET y que hasta ahora era "validación manual post-deploy".
#
# Si Docker no está disponible (CI sin runner, máquina sin Docker), los tests
# marcados `@pytest.mark.integration` se SKIPEAN — no fallan. Así la suite
# rápida sigue verde en cualquier lado.


@pytest.fixture(scope="session")
def pg_container():
    """Levanta un postgres:16 efímero para toda la sesión de tests.

    Devuelve el objeto PostgresContainer ya arrancado. scope=session →
    un solo contenedor para todos los tests de integración (arranque ~3-5s,
    se amortiza). Se apaga solo al terminar la corrida.

    SKIP automático si testcontainers no está instalado o Docker no responde.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers no instalado (pip install -e '.[dev]')")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as e:  # Docker no corriendo / sin permisos / sin imagen
        pytest.skip(f"Docker no disponible para tests de integración: {e}")

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
def pg_conn_info(pg_container):
    """`PostgresConnInfo` apuntando al contenedor efímero.

    Es lo que consume `QueryPostgresTool`. Lee el host/puerto/credenciales
    expuestos por testcontainers (el puerto es aleatorio por corrida).
    """
    from harness.tool.ecosystem.postgres import PostgresConnInfo

    return PostgresConnInfo(
        host=pg_container.get_container_host_ip(),
        port=int(pg_container.get_exposed_port(5432)),
        user=pg_container.username,
        password=pg_container.password,
        database=pg_container.dbname,
    )
