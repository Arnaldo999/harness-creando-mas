"""ProviderRouter — failover automático con retry y logging estructurado.

Política:
1. Llama al provider primario (OpenAI).
2. Si tira:
   - 5xx (`openai.InternalServerError`, `openai.APIServerError`)
   - rate limit (`openai.RateLimitError`)
   - timeout (`openai.APITimeoutError`, asyncio.TimeoutError)
   - cualquier excepción transient (httpx errores, ConnectionError, ...)
   → fallback al provider de fallback (Gemini).
3. Si Gemini también falla → propagar `ProvidersExhaustedError` que el
   endpoint /chat traduce a respuesta controlada.

NO reintenta llamados sobre el mismo provider — confía en `max_retries=2`
ya configurado en el cliente OpenAI/Gemini. El router solo decide
"otro provider o me rindo".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from harness.api import Message, Response, ToolDef
from harness.provider.base import Provider

log = logging.getLogger(__name__)


class ProvidersExhaustedError(RuntimeError):
    """Todos los providers fallaron. El layer HTTP lo traduce a 503."""


# Excepciones que disparan failover. Mantenemos la lista por nombre para
# no acoplar el router al SDK de OpenAI (que podría no estar instalado
# en algún path de tests). Si el nombre de la clase coincide, hacemos failover.
_FAILOVER_EXC_NAMES = {
    "RateLimitError",
    "APITimeoutError",
    "APIConnectionError",
    "APIError",
    "InternalServerError",
    "APIServerError",
    "BadGatewayError",
    "ServiceUnavailableError",
    "ConnectError",
    "ReadTimeout",
    "WriteTimeout",
    "ConnectTimeout",
    "PoolTimeout",
}


def _should_failover(exc: BaseException) -> bool:
    """Decide si una excepción del provider debe disparar failover.

    Reglas:
    - asyncio.TimeoutError / TimeoutError → sí.
    - Excepciones con nombre en _FAILOVER_EXC_NAMES → sí.
    - 5xx (status_code 500..599) → sí.
    - Resto → no (probablemente bug del harness o input inválido; fallar
      en silencio sobre Gemini puede ocultar el problema real).
    """
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return True
    if type(exc).__name__ in _FAILOVER_EXC_NAMES:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    # OpenAI APIStatusError-like: tiene .response.status_code
    response = getattr(exc, "response", None)
    if response is not None:
        rstatus = getattr(response, "status_code", None)
        if isinstance(rstatus, int) and (500 <= rstatus < 600 or rstatus == 429):
            return True
    return False


class ProviderRouter(Provider):
    """Implementa Provider envolviendo dos providers con failover.

    Es un Provider en sí mismo — el agent loop no se entera de que adentro
    hay dos. Esto mantiene el contract limpio.
    """

    def __init__(
        self,
        primary: Provider,
        fallback: Provider | None = None,
        *,
        total_timeout: float = 110.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._last_used: str = primary.name
        self._total_timeout = total_timeout

    @property
    def name(self) -> str:
        return f"router({self._primary.name}+{self._fallback.name if self._fallback else 'none'})"

    @property
    def model(self) -> str:
        # Devolvemos el del último usado para que el visor de modelos
        # refleje qué corrió de verdad.
        return self._primary.model if self._last_used == self._primary.name else (
            self._fallback.model if self._fallback else self._primary.model
        )

    @property
    def system(self) -> str:
        return self._primary.system

    @system.setter
    def system(self, value: str) -> None:
        self._primary.system = value
        if self._fallback is not None:
            self._fallback.system = value

    @property
    def last_used(self) -> str:
        """Nombre del provider que respondió la última llamada exitosa.
        Usado por el endpoint /chat para popular `ChatResponse.provider_used`.
        """
        return self._last_used

    async def send(self, messages: list[Message], tools: list[ToolDef]) -> Response:
        primary_start = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._primary.send(messages, tools),
                timeout=self._total_timeout,
            )
            self._last_used = self._primary.name
            return resp
        except BaseException as exc:
            latency_ms = (time.perf_counter() - primary_start) * 1000
            if not _should_failover(exc) or self._fallback is None:
                # No es un error "fallover-able" o no hay backup → re-lanzar.
                if self._fallback is None:
                    log.error(
                        "provider_primary_failed_no_fallback",
                        extra={
                            "provider": self._primary.name,
                            "error_type": type(exc).__name__,
                            "error_msg": str(exc)[:200],
                            "latency_ms": round(latency_ms, 1),
                        },
                    )
                raise
            log.warning(
                "provider_failover",
                extra={
                    "primary": self._primary.name,
                    "fallback": self._fallback.name,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc)[:200],
                    "latency_ms": round(latency_ms, 1),
                },
            )

        # Intento con el fallback.
        fb_start = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._fallback.send(messages, tools),
                timeout=self._total_timeout,
            )
            self._last_used = self._fallback.name
            return resp
        except BaseException as exc:
            log.error(
                "provider_fallback_also_failed",
                extra={
                    "fallback": self._fallback.name,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc)[:200],
                    "latency_ms": round((time.perf_counter() - fb_start) * 1000, 1),
                },
            )
            raise ProvidersExhaustedError(
                f"primary={self._primary.name} and fallback={self._fallback.name} both failed"
            ) from exc


__all__ = ["ProviderRouter", "ProvidersExhaustedError", "_should_failover"]
