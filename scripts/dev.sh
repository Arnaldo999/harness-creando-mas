#!/usr/bin/env bash
# Arranca uvicorn en modo dev (reload) escuchando en localhost:8000.
# Usage: ./scripts/dev.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Activar venv si existe.
if [[ -d "venv" ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
elif [[ -d ".venv" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec uvicorn harness.api.app:create_app --factory --reload --port 8000 --host 0.0.0.0
