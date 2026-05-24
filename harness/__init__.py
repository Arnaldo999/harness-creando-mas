"""harness-creando-mas — paquete raíz del harness HTTP multi-tenant.

Adaptación del proyecto educativo `byo-harness-python` a una pieza
operacional de la agencia Creando Más / Lovbot:

- Layer HTTP (FastAPI) en `harness.api`.
- Provider router con failover OpenAI → Gemini en `harness.provider.router`.
- Tools del negocio (Postgres, lead helpers, Tavily) en `harness.tool.ecosystem`.
- Configuración por tenant en `harness.tenant` (lee `tenants/<slug>/`).
- Memoria conversacional en `harness.session` (in-memory por ahora).
"""

__version__ = "0.1.0"
