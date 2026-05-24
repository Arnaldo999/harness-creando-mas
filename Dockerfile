# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# harness-creando-mas — imagen de producción
# -----------------------------------------------------------------------------
# Base slim para minimizar superficie. Si en algún momento necesitamos
# librerías de sistema (ej. para libpq nativo), pasarlo a `python:3.12`.
# asyncpg no requiere libpq — es puro Python sobre el protocolo Postgres.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias mínimas (curl para healthcheck).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Capa de dependencias separada para aprovechar el cache de Docker.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copiar el código.
COPY harness ./harness
COPY tenants ./tenants
COPY pyproject.toml ./

# Usuario no-root.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "harness.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
