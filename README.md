# harness-creando-mas

Harness HTTP multi-tenant de la agencia **Creando Más** / Lovbot.ai.
Fase 0 — demo inmobiliario (`tenants/demo`).

Reemplazo de `POST n8n.lovbot.ai/webhook/crm-ia-chat` por un servicio
FastAPI propio, deployable en Coolify Hetzner como container independiente.
La meta es sacar el chat IA del n8n monolítico (blast radius enorme) y
ponerlo bajo dominio propio: `https://agente.lovbot.ai/chat`.

---

## Arquitectura (Fase 0)

```
crm-v2.html (browser)
        │
        │  POST /chat { message, tenant_slug, session_id }
        ▼
┌─────────────────────────────────────────┐
│ harness-creando-mas (FastAPI)           │
│                                         │
│  api/routes/chat.py                     │
│      │                                  │
│      ▼                                  │
│  agent/agent.py  (async loop)           │
│      │                                  │
│      ▼                                  │
│  provider/router.py  ┐                  │
│      │ try OpenAI    │ failover         │
│      └─► Gemini      ┘                  │
│      │                                  │
│      ▼                                  │
│  tool/ecosystem/                        │
│   - query_postgres                      │
│   - lookup_lead                         │
│   - update_lead_estado                  │
│   - generar_resumen_conversacion        │
│   - tavily_search                       │
└─────────────────────────────────────────┘
        │                       │
        ▼                       ▼
  Postgres `lovbot_crm_modelo`  Tavily / OpenAI / Gemini
  (red interna Docker)
```

Cada cliente vive en `tenants/<slug>/` con tres archivos:
- `system_prompt.md`
- `tools.yaml`
- `data_sources.yaml`

El loader lee esos archivos al arrancar (por demanda), resuelve env vars
referenciadas por nombre, y arma un `TenantBundle` (provider + registry de
tools). Se cachea en `app.state.tenant_bundles`.

---

## Setup local

Requiere Python 3.12+.

```bash
cd "/home/arna/PROYECTO CREANDO MAS/harness-creando-mas"

# 1. Venv + deps
python3 -m venv venv
source venv/bin/activate
pip install -e .[dev]

# 2. Variables de entorno
cp .env.example .env
# editar .env con LOVBOT_OPENAI_API_KEY, LOVBOT_PG_*, etc.

# 3. Tests
pytest -v

# 4. Dev server
./scripts/dev.sh
# → uvicorn en http://localhost:8000
# → docs interactivas: http://localhost:8000/docs
```

### Smoke test rápido

```bash
# Health
curl -s http://localhost:8000/health | jq

# Chat sin auth (modo dev, LOVBOT_AGENTE_API_KEY desactivado)
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"¿Cuántos leads calientes tengo?","tenant_slug":"demo"}' | jq

# Chat con auth
curl -s -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $LOVBOT_AGENTE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hola","tenant_slug":"demo"}' | jq
```

---

## Deploy en Coolify (Hetzner Robert)

1. **DNS**: crear `A agente.lovbot.ai → 5.161.235.99` en prodns.mx (TTL 300).
2. **Crear app en Coolify Hetzner**:
   - Project: `lovbot-projects`
   - Name: `lovbot-agente-harness`
   - Source: este repo (`harness-creando-mas`, branch `main`)
   - Build pack: Docker Compose (lee `docker-compose.yml`)
3. **Variables de entorno**: pegar el contenido de `.env.example` y completar:
   - `LOVBOT_AGENTE_API_KEY` (generar nuevo con `openssl rand -hex 32`)
   - `LOVBOT_OPENAI_API_KEY` (de la agencia Lovbot)
   - `LOVBOT_GEMINI_API_KEY` (alias del `GEMINI_API_KEY` existente)
   - `LOVBOT_PG_PASS` (del compose Postgres `p8s8kcgckgoc484wwo4w8wck`)
   - `TAVILY_API_KEY` (de la cuenta agencia)
4. **Networks externas** ya están declaradas en el compose. Coolify las
   adjunta automáticamente:
   - `coolify` (proxy Traefik público).
   - `p8s8kcgckgoc484wwo4w8wck_default` (red interna del Postgres).
5. **Deploy**. Esperar healthcheck verde.
6. **Verificar**:
   ```bash
   curl -sf https://agente.lovbot.ai/health
   curl -s -X POST https://agente.lovbot.ai/chat \
     -H "Authorization: Bearer $LOVBOT_AGENTE_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"message":"¿qué propiedades disponibles tengo?","tenant_slug":"demo"}'
   ```
7. **Switch del CRM**: en `crm-v2.html:7282` (aprox) cambiar
   `n8n.lovbot.ai/webhook/crm-ia-chat` → `agente.lovbot.ai/chat`.
   Sumar el header `Authorization: Bearer <LOVBOT_AGENTE_API_KEY>`.
   Mantener el workflow n8n vivo como rollback durante 1-2 semanas.

### Ventana de deploy

El harness es un container independiente; **no aplica** la regla 23-06hs ARG
del backend monolítico Coolify Hostinger. Se puede deployar en cualquier
hora porque su único upstream es Postgres (que no es afectado por su
restart) y los clientes externos son operadores del CRM (no bots WA en
producción).

---

## Estructura del proyecto

```
harness-creando-mas/
├── README.md
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── harness/
│   ├── __init__.py
│   ├── agent/                 # Agent loop async
│   ├── api/                   # FastAPI app + routers + schemas
│   │   ├── app.py             # create_app()
│   │   ├── deps.py            # Depends (auth, session store)
│   │   ├── schemas.py         # Pydantic models (ChatRequest, ChatResponse)
│   │   ├── types.py           # tipos LLM-agnósticos (Message, Block, ...)
│   │   └── routes/
│   │       ├── chat.py
│   │       └── health.py
│   ├── provider/
│   │   ├── base.py            # Interfaz Provider (async)
│   │   ├── openai_provider.py
│   │   ├── gemini_provider.py
│   │   ├── router.py          # Failover OpenAI → Gemini
│   │   └── mock.py            # Para tests
│   ├── tool/
│   │   ├── registry.py        # Registry + interfaz Tool
│   │   └── ecosystem/         # Tools del negocio Creando Más
│   │       ├── postgres.py    # query_postgres + SQL validator
│   │       ├── lead.py        # lookup_lead, update_lead_estado
│   │       ├── conversation.py
│   │       └── tavily.py
│   ├── tenant/
│   │   ├── config.py          # TenantConfig dataclass
│   │   ├── loader.py          # build_tenant_bundle()
│   │   └── auth.py            # bearer + tenant resolution
│   └── session/
│       └── store.py           # SessionStore in-memory TTL 1h
├── tenants/
│   └── demo/                  # Demo Lovbot inmobiliario
│       ├── system_prompt.md
│       ├── tools.yaml
│       └── data_sources.yaml
├── tests/
│   ├── test_postgres_tool.py  # validator SQL + tenant injection
│   ├── test_tenant_loader.py
│   ├── test_provider_router.py
│   └── test_chat_endpoint.py
└── scripts/
    └── dev.sh
```

---

## Decisiones clave

- **Sin `default_registry` global** (a diferencia del `byo-harness-python`):
  cada tenant tiene SU propio Registry porque la lista de tools enabled
  cambia por cliente.
- **Tools del CLI educativo (bash, read_file, write_file) NO se cargan**
  en modo HTTP — por seguridad y para acotar la superficie del LLM al
  dominio CRM.
- **Async todo el stack** (FastAPI, asyncpg, httpx, OpenAI/Gemini async
  SDKs) para no bloquear el event loop.
- **Validador SQL con `\b` word boundaries** — fix del bug histórico del
  workflow n8n donde el regex sin boundaries matcheaba "CREATE" contra
  columnas `created_at`.
- **Tenant injection conservadora**: solo inyecta `tenant_slug = '...'`
  si la query referencia una tabla que físicamente tiene esa columna
  (descubrimiento vía `information_schema.columns`). La separación
  primaria del demo es DB-per-cliente; esto es defensa en profundidad.
- **Bearer auth opcional en dev**: si `LOVBOT_AGENTE_API_KEY` no está
  seteado, el server arranca en modo open (con warn en logs).

---

## Pendientes / Fase 1+

- Generación de resúmenes de conversación on-demand (hoy solo lookup).
- Session store en Postgres (hoy in-memory TTL 1h).
- Streaming SSE en /chat.
- Métricas Prometheus (/metrics).
- Tools de WhatsApp outbound (Meta Graph API).
- Tools de gestión Airtable (para agencia Arnaldo).
- JWT por tenant (en vez de bearer global).
- Replicar el patrón a `tenants/arnaldo-*/` y `tenants/mica-*/`.
