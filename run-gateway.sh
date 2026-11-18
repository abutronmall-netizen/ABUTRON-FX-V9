#!/usr/bin/env bash
# ABUTRON MT5 Gateway — multi-tenant REST + WebSocket bridge.
# Binds 0.0.0.0 so it works behind the platform preview proxy.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/src"
export ABUTRON_GATEWAY_HOST="${ABUTRON_GATEWAY_HOST:-0.0.0.0}"
export ABUTRON_GATEWAY_PORT="${ABUTRON_GATEWAY_PORT:-19744}"

PYTHON="${PYTHON:-python3}"
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
fi

exec "$PYTHON" -m abutron.gateway
