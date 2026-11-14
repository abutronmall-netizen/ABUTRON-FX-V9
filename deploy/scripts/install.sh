#!/usr/bin/env bash
# ABUTRON FX V9 — Ubuntu 24.04 host installer.
# Sets up Wine + MT5 terminals, Python deps, systemd units and data dirs.
# Safe to re-run. Never embeds broker credentials.
set -euo pipefail

APP_USER="${ABUTRON_USER:-abutron}"
APP_DIR="${ABUTRON_APP_DIR:-/opt/abutron-fx-v9}"
DATA_DIR="${ABUTRON_DATA_DIR:-/var/lib/abutron-fx-v9/data}"
SRC_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "==> ABUTRON FX V9 installer"
echo "    app dir : $APP_DIR"
echo "    data dir: $DATA_DIR"

# 1. System packages: Wine + tools
echo "==> installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    wine64 wine32  wine-binfmt ca-certificates curl gnupg2 \
    python3 python3-pip python3-venv systemd ubuntu-keyring

# 2. Python venv + deps
echo "==> python environment"
if [ ! -d "$APP_DIR/.venv" ]; then
    sudo mkdir -p "$APP_DIR"
    sudo cp -r "$SRC_DIR/." "$APP_DIR/"
    sudo useradd -r -m -d "$DATA_DIR" "$APP_USER" 2>/dev/null || true
    sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# 3. Data dirs (root-owned, native evidence)
echo "==> data directories"
sudo mkdir -p "$DATA_DIR"/{native,decisions,receipts,ledger,state,mt5}
sudo chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

# 4. MT5 terminal directory + Wine prefixes
echo "==> wine/MT5 runtime"
sudo -u "$APP_USER" mkdir -p "$DATA_DIR/mt5"
sudo -u "$APP_USER" env WINEPREFIX="$DATA_DIR/mt5/prefix-bootstrap" \
    wineboot --init 2>/dev/null || true

# 5. systemd units
echo "==> systemd units"
sudo cp "$SRC_DIR"/deploy/systemd/*.service /etc/systemd/system/
sudo cp "$SRC_DIR"/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable abutron-portal.service abutron-dispatch.service \
    abutron-reconcile.timer abutron-health.timer

echo "==> done. Next:"
echo "    sudo systemctl start abutron-portal.service"
echo "    sudo systemctl start abutron-mt5-worker@<connection_id>"
echo "    Portal: http://<host>:${ABUTRON_PORTAL_PORT:-19743}/"
