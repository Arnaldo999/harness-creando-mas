"""RateLimiter — counters per-tenant en tres ventanas (minute/hour/day).

Diseño:
- Tres `TTLCache` separados, uno por ventana. El TTL del cache hace de
  expiración automática: una key que no se toca en 60s desaparece de la
  cache de "minute", etc. Esto evita tener que correr un cron de cleanup.
- Cada entry es un dict `{count: int, window_start: float}`. Aunque
  `TTLCache` no soporta mutar el valor in-place de forma atómica entre
  threads (de hecho, NO es thread-safe), envolvemos toda operación con
  un `asyncio.Lock` — el patrón ya validado en `ResponseCache`.
- `retry_after_seconds` se calcula como el tiempo que falta para que
  expire la entry actual de la ventana: si entraron al minuto en t=10s
  y se cortó en t=45s, le decimos "esperá 15s" (no 60).

Por qué TTLCache en vez de `defaultdict(int)` + cleanup manual:
- Cero código de eviction → menos bugs.
- `maxsize` bounded → cap de memoria duro aunque crezcan los tenants.
- Probado en producción (mismo módulo que `harness.cache`).

Tradeoff: si una entry expira mid-check (race entre `cache.get` y
`time.time()`), tratamos como "primer hit en una ventana nueva". Es el
comportamiento correcto desde el punto de vista del usuario.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Literal

from cachetools import TTLCache

Window = Literal["minute", "hour", "day"]

# Duración en segundos de cada ventana. Usados también como TTL de cada
# TTLCache — si una key no se toca durante toda la ventana, desaparece
# sola.
_WINDOW_SECONDS: dict[Window, int] = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


@dataclass
class RateLimitsConfig:
    """Límites declarativos para un tenant. `None` en cualquier campo
    significa "esa ventana no se chequea" — tenant sin límite en esa
    granularidad.
    """

    per_minute: int | None = None
    per_hour: int | None = None
    per_day: int | None = None

    def is_unlimited(self) -> bool:
        """True si no hay ningún chequeo activo (las tres en None).

        En ese caso el RateLimiter ni siquiera adquiere el lock.
        """
        return (
            self.per_minute is None
            and self.per_hour is None
            and self.per_day is None
        )


@dataclass
class RateLimitResult:
    """Resultado de un check.

    - `allowed=True` y `exceeded_window=None` cuando pasa.
    - `allowed=False`, `exceeded_window` y `retry_after_seconds` cuando
      se excedió alguna ventana. La primera ventana en violarse es la
      que se reporta (el orden de chequeo es minute → hour → day).
    - `current` siempre tiene el conteo actualizado por ventana
      (incluida la que falló), útil para el mensaje al usuario.
    """

    allowed: bool
    exceeded_window: Window | None = None
    retry_after_seconds: int | None = None
    current: dict[str, int] = field(default_factory=dict)


@dataclass
class _Counter:
    """Counter mutable guardado dentro de cada TTLCache.

    `window_start` es el `time.time()` del primer hit de esta ventana.
    Sirve para calcular el `retry_after` exacto.
    """

    count: int
    window_start: float


class RateLimiter:
    """Rate limiter in-memory, async-safe, multi-tenant.

    Una sola instancia atiende a TODOS los tenants — el scoping se hace
    via la `key` (típicamente `f"{tenant_slug}:{user_id}"`).
    """

    def __init__(self, *, maxsize: int = 10_000) -> None:
        # TTLCache por ventana. El TTL = duración de la ventana, así
        # cuando expira el entry, automáticamente equivale a "ventana
        # nueva" en el próximo hit.
        self._caches: dict[Window, TTLCache[str, _Counter]] = {
            "minute": TTLCache(maxsize=maxsize, ttl=_WINDOW_SECONDS["minute"]),
            "hour": TTLCache(maxsize=maxsize, ttl=_WINDOW_SECONDS["hour"]),
            "day": TTLCache(maxsize=maxsize, ttl=_WINDOW_SECONDS["day"]),
        }
        self._lock = asyncio.Lock()
        self._maxsize = maxsize

    @property
    def maxsize(self) -> int:
        return self._maxsize

    async def check_and_record(
        self, key: str, limits: RateLimitsConfig
    ) -> RateLimitResult:
        """Atómicamente: si las tres ventanas pasan, incrementa todos
        los counters y devuelve `allowed=True`. Si alguna ventana
        excede, NO incrementa nada (no penaliza por el intento) y
        devuelve `allowed=False` con detalles.

        El orden de chequeo es minute → hour → day. La primera que
        falla es la que se reporta como `exceeded_window`.
        """
        # Sin límites configurados → fast path sin tomar el lock.
        if limits.is_unlimited():
            return RateLimitResult(allowed=True, current={})

        now = time.time()

        async with self._lock:
            # Snapshot del estado actual (sin mutar todavía).
            snapshots: dict[Window, _Counter | None] = {
                "minute": self._caches["minute"].get(key),
                "hour": self._caches["hour"].get(key),
                "day": self._caches["day"].get(key),
            }

            # Si pasáramos, ¿en cuánto quedaría cada counter?
            projected: dict[str, int] = {}
            for window in ("minute", "hour", "day"):
                snap = snapshots[window]  # type: ignore[index]
                projected[window] = (snap.count + 1) if snap is not None else 1

            # Chequear cada ventana en orden. La primera que viole corta.
            limit_for = {
                "minute": limits.per_minute,
                "hour": limits.per_hour,
                "day": limits.per_day,
            }
            for window in ("minute", "hour", "day"):
                cap = limit_for[window]
                if cap is None:
                    continue
                if projected[window] > cap:
                    # Exceeded. No incrementamos, pero devolvemos el
                    # estado actual (sin contar el intento bloqueado).
                    snap = snapshots[window]  # type: ignore[index]
                    retry = self._compute_retry_after(window, snap, now)
                    current_no_inc = {
                        w: (snapshots[w].count if snapshots[w] is not None else 0)  # type: ignore[union-attr,index]
                        for w in ("minute", "hour", "day")
                    }
                    return RateLimitResult(
                        allowed=False,
                        exceeded_window=window,  # type: ignore[arg-type]
                        retry_after_seconds=retry,
                        current=current_no_inc,
                    )

            # Pasó las tres → commit del incremento.
            for window in ("minute", "hour", "day"):
                snap = snapshots[window]  # type: ignore[index]
                if snap is None:
                    self._caches[window][key] = _Counter(  # type: ignore[index]
                        count=1, window_start=now
                    )
                else:
                    snap.count += 1
                    # Re-insertamos para refrescar el orden interno del
                    # cache (TTLCache puede haber recordado la entry
                    # original sin actualizar acceso si solo mutamos).
                    self._caches[window][key] = snap  # type: ignore[index]

            return RateLimitResult(
                allowed=True,
                current={
                    "minute": projected["minute"] if limits.per_minute is not None else 0,
                    "hour": projected["hour"] if limits.per_hour is not None else 0,
                    "day": projected["day"] if limits.per_day is not None else 0,
                },
            )

    @staticmethod
    def _compute_retry_after(
        window: Window, snap: _Counter | None, now: float
    ) -> int:
        """Segundos hasta que se libere la ventana exceeded.

        Si no hay snapshot (raro: significa que projected=1 ya excedió,
        lo cual implica `limit < 1`), devolvemos la ventana completa.
        """
        window_len = _WINDOW_SECONDS[window]
        if snap is None:
            return window_len
        elapsed = now - snap.window_start
        remaining = window_len - elapsed
        if remaining <= 0:
            # Edge case: la ventana ya expiró pero leímos un snapshot
            # stale (race con TTL). Mínimo 1s para no devolver 0.
            return 1
        # Redondeamos hacia arriba — mejor decirle "esperá 16s" que
        # "esperá 15s" y que reintente y vuelva a fallar.
        return max(1, math.ceil(remaining))

    async def reset(self, key: str | None = None) -> None:
        """Limpia counters. Si `key` es None → limpia todo (útil en
        tests). Si es una key específica → solo esa.
        """
        async with self._lock:
            if key is None:
                for cache in self._caches.values():
                    cache.clear()
                return
            for cache in self._caches.values():
                cache.pop(key, None)


def format_retry_after_human(seconds: int | None) -> str:
    """Convierte segundos a un string legible en español rioplatense.

    Ejemplos: 30 → "30 segundos", 60 → "1 minuto", 90 → "2 minutos"
    (redondeo hacia arriba para no quedar corto), 3600 → "1 hora",
    7200 → "2 horas", 86400 → "1 día".

    Diseñado para meter directo en el mensaje al usuario:
    "esperá {format_retry_after_human(n)} antes de volver a consultar".
    """
    if seconds is None or seconds <= 0:
        return "unos segundos"
    if seconds < 60:
        return f"{seconds} segundo{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = math.ceil(seconds / 60)
        return f"{minutes} minuto{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = math.ceil(seconds / 3600)
        return f"{hours} hora{'s' if hours != 1 else ''}"
    days = math.ceil(seconds / 86400)
    return f"{days} día{'s' if days != 1 else ''}"


def format_window_human(window: Window | str | None) -> str:
    """Traduce el nombre de la ventana al español usado en el mensaje
    al usuario: 'minute' → 'último minuto', 'hour' → 'última hora',
    'day' → 'último día'.
    """
    mapping = {
        "minute": "último minuto",
        "hour": "última hora",
        "day": "último día",
    }
    return mapping.get(str(window), "ventana actual")


__all__ = [
    "RateLimiter",
    "RateLimitResult",
    "RateLimitsConfig",
    "Window",
    "format_retry_after_human",
    "format_window_human",
]
