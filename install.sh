#!/usr/bin/env bash
# 3D Printer Open Workbench ERP — idempotent installer (safe to run again).
#
#   ./install.sh             create .venv, install pinned dependencies, create .env and data/
#   ./install.sh --dev       also install test dependencies (pytest, httpx)
#   ./install.sh --service   also install and start the systemd --user unit "workbench-erp"
#   ./install.sh --test      run the test suite at the end
set -euo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$APP_DIR"

DEV=0; SERVICE=0; TEST=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    --service) SERVICE=1 ;;
    --test) DEV=1; TEST=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

# 1. Python
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. On Ubuntu: sudo apt install python3 python3-venv" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required (found $("$PY" --version 2>&1))." >&2
  exit 1
fi

# 2. Virtual environment (reused if it already exists)
if [ ! -x .venv/bin/python ]; then
  say "Creating virtual environment in .venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PY" .venv
  else
    "$PY" -m venv .venv || {
      echo "venv creation failed. On Ubuntu: sudo apt install python3-venv" >&2
      exit 1
    }
  fi
else
  say "Reusing existing .venv"
fi

# 3. Dependencies
say "Installing dependencies (requirements.lock)"
REQS=(-r requirements.lock)
[ "$DEV" = 1 ] && REQS+=(-r requirements-dev.txt)
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python "${REQS[@]}"
else
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install "${REQS[@]}"
fi

# 4. Configuration and data folder (never overwritten)
if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  cp .env.example .env
else
  say "Keeping existing .env"
fi
mkdir -p data

# 5. Initialise the database (creates tables; adds missing columns on upgrades)
say "Initialising database"
.venv/bin/python -c 'import database, workbench_config as c; database.init_db_and_seed(); print("Database:", c.DB_PATH)'

# 6. Optional systemd --user service
if [ "$SERVICE" = 1 ]; then
  say "Installing systemd user service: workbench-erp"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  sed "s|@APP_DIR@|$APP_DIR|g" deploy/workbench-erp.service.in > "$UNIT_DIR/workbench-erp.service"
  systemctl --user daemon-reload
  systemctl --user enable --now workbench-erp.service
  systemctl --user restart workbench-erp.service
  systemctl --user --no-pager --lines=0 status workbench-erp.service || true
  echo "Tip: 'sudo loginctl enable-linger $USER' keeps it running after logout."
fi

# 7. Optional tests
if [ "$TEST" = 1 ]; then
  say "Running tests"
  .venv/bin/python -m pytest -q
fi

PORT="$(. ./.env 2>/dev/null; echo "${WORKBENCH_ERP_PORT:-8000}")"
cat <<EOF

Done.
  Start (foreground):   ./run.sh
  Web UI:               http://127.0.0.1:${PORT}
  API docs:             http://127.0.0.1:${PORT}/docs
  MCP for Claude Code:  claude mcp add workbench-erp -- "$APP_DIR/.venv/bin/python" "$APP_DIR/mcp_server.py"
EOF
