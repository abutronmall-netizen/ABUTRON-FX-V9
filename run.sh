#!/usr/bin/env bash
# ABUTRON FX V9 — launch the portal + APEX engine runtime.
# Binds 0.0.0.0 so it works behind the platform preview proxy.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/src"
export ABUTRON_PORTAL_HOST="${ABUTRON_PORTAL_HOST:-0.0.0.0}"
export ABUTRON_PORTAL_PORT="${ABUTRON_PORTAL_PORT:-19743}"

PYTHON="${PYTHON:-python3}"
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
fi

exec "$PYTHON" -m abutron.main
