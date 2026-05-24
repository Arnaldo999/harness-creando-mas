"""Tests del ResponseCache — TTL, write-tool skip, normalización, thread safety."""

from __future__ import annotations

import asyncio

import pytest

from harness.cache import CachedResponse, ResponseCache, normalize_key
from harness.cache.store import WRITE_TOOL_NAMES


class TestNormalizeKey:
    def test_lowercase(self) -> None:
        assert normalize_key("Hola Mundo") == "hola mundo"

    def test_strip(self) -> None:
        assert normalize_key("  hola  ") == "hola"

    def test_colapsa_whitespace(self) -> None:
        assert normalize_key("hola    mundo\t\nche") == "hola mundo che"

    def test_empty(self) -> None:
        assert normalize_key("") == ""

    def test_make_key_incluye_tenant(self) -> None:
        k1 = ResponseCache.make_key("demo", "Hola")
        k2 = ResponseCache.make_key("otro", "Hola")
        assert k1 != k2
        assert k1 == "demo:hola"


class TestResponseCacheBasics:
    @pytest.mark.asyncio
    async def test_put_y_get(self) -> None:
        cache = ResponseCache(maxsize=10, ttl_seconds=60)
        ok = await cache.put(
            "demo:hola",
            {"respuesta": "ok", "tokens_in": 100, "tokens_out": 20, "provider_used": "openai"},
            tool_names_used=["query_postgres"],
        )
        assert ok is True
        hit = await cache.get("demo:hola")
        assert hit is not None
        assert hit.respuesta == "ok"
        assert hit.tokens_in == 100
        assert hit.tokens_out == 20
        assert hit.provider_used == "openai"

    @pytest.mark.asyncio
    async def test_miss_devuelve_none(self) -> None:
        cache = ResponseCache()
        assert await cache.get("demo:nada") is None

    @pytest.mark.asyncio
    async def test_acepta_cached_response_directo(self) -> None:
        cache = ResponseCache()
        cr = CachedResponse(respuesta="hello", tokens_in=5, provider_used="mock")
        await cache.put("demo:k", cr, tool_names_used=[])
        hit = await cache.get("demo:k")
        assert hit is not None
        assert hit.respuesta == "hello"
        assert hit.provider_used == "mock"


class TestTTLExpiry:
    @pytest.mark.asyncio
    async def test_ttl_expira(self) -> None:
        cache = ResponseCache(maxsize=10, ttl_seconds=1)
        await cache.put(
            "demo:hola", {"respuesta": "ok"}, tool_names_used=[]
        )
        # Inmediato → hit.
        assert await cache.get("demo:hola") is not None
        # Esperar TTL + epsilon.
        await asyncio.sleep(1.2)
        assert await cache.get("demo:hola") is None


class TestWriteToolSkip:
    @pytest.mark.asyncio
    async def test_skip_si_tool_de_escritura(self) -> None:
        cache = ResponseCache()
        ok = await cache.put(
            "demo:k",
            {"respuesta": "ok"},
            tool_names_used=["update_lead_estado"],
        )
        assert ok is False
        assert await cache.get("demo:k") is None

    @pytest.mark.asyncio
    async def test_cache_si_solo_tools_read_only(self) -> None:
        cache = ResponseCache()
        ok = await cache.put(
            "demo:k",
            {"respuesta": "ok"},
            tool_names_used=["query_postgres", "lookup_lead", "tavily_search"],
        )
        assert ok is True
        assert await cache.get("demo:k") is not None

    @pytest.mark.asyncio
    async def test_cache_si_lista_de_tools_vacia(self) -> None:
        cache = ResponseCache()
        ok = await cache.put("demo:k", {"respuesta": "ok"}, tool_names_used=[])
        assert ok is True

    def test_write_tool_names_incluye_update_lead(self) -> None:
        assert "update_lead_estado" in WRITE_TOOL_NAMES


class TestThreadSafetyAsync:
    @pytest.mark.asyncio
    async def test_gather_concurrent_put_get(self) -> None:
        """Hammer concurrente para detectar race conditions obvias."""
        cache = ResponseCache(maxsize=500, ttl_seconds=60)

        async def writer(i: int) -> None:
            await cache.put(
                f"demo:k{i}", {"respuesta": f"resp{i}"}, tool_names_used=[]
            )

        async def reader(i: int) -> None:
            await cache.get(f"demo:k{i}")

        tasks = []
        for i in range(50):
            tasks.append(writer(i))
            tasks.append(reader(i))
        await asyncio.gather(*tasks)

        # Verificamos que al menos 40/50 quedaron escritos (el reader
        # puede haber corrido antes que el writer en algunos, eso es OK).
        present = 0
        for i in range(50):
            if await cache.get(f"demo:k{i}") is not None:
                present += 1
        assert present == 50
        assert await cache.size() == 50


class TestKeyNormalizationIntegration:
    @pytest.mark.asyncio
    async def test_misma_pregunta_distinto_case_misma_key(self) -> None:
        cache = ResponseCache()
        k1 = ResponseCache.make_key("demo", "Cuántos leads calientes tengo?")
        k2 = ResponseCache.make_key("demo", "cuántos   leads CALIENTES tengo?")
        assert k1 == k2
        await cache.put(k1, {"respuesta": "12"}, tool_names_used=[])
        assert (await cache.get(k2)) is not None
