#!/usr/bin/env bash
# Starts the web UI + REST API. Usage: ./run.sh [--no-reload]
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
HOST="${WORKBENCH_ERP_HOST:-127.0.0.1}"
PORT="${WORKBENCH_ERP_PORT:-8000}"
RELOAD=(--reload)
[ "${1:-}" = "--no-reload" ] && RELOAD=()
exec .venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT" "${RELOAD[@]}"
