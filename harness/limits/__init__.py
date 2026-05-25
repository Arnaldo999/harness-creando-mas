"""Rate limiting per-tenant del harness.

Protege la factura de OpenAI del demo público — sin esto, el primer
abuser que encuentre el endpoint nos come miles de tokens. Los límites
los define cada tenant en su `data_sources.yaml` (block opcional
`rate_limits`); si el block no existe, el tenant queda sin límite (caso
producción facturable al cliente).
"""

from harness.limits.rate_limiter import (
    RateLimiter,
    RateLimitResult,
    RateLimitsConfig,
    format_retry_after_human,
    format_window_human,
)

__all__ = [
    "RateLimiter",
    "RateLimitResult",
    "RateLimitsConfig",
    "format_retry_after_human",
    "format_window_human",
]
