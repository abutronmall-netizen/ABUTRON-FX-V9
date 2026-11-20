#!/usr/bin/env bash
# ABUTRON FX V9 — link external data + optional vendor into the workspace.
#
# Clones (or updates) the public market-data and trade-history repositories
# into data/links/, which is where src/abutron/config.py looks by default:
#   ABUTRON_MARKET_DATA_DIR   -> data/links/market-data
#   ABUTRON_TRADE_HISTORY_DIR -> data/links/trade-history
#
# Usage:
#   bash scripts/link_all.sh [--update] [--vendor]
#
#   --update   git pull the linked repos instead of skipping existing clones
#   --vendor   also clone the optional vendored APEX source into
#              vendor/abutron-apex (apex_integration.py looks there; the
#              platform runs without it on the NexusBrain + apex engine)
#
# Safe to re-run. Never embeds credentials. data/ is git-ignored, so the
# linked datasets are never committed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINKS="$ROOT/data/links"
UPDATE=0
VENDOR=0
for arg in "$@"; do
    case "$arg" in
        --update) UPDATE=1 ;;
        --vendor) VENDOR=1 ;;
        *) echo "unknown arg: $arg (usage: link_all.sh [--update] [--vendor])" >&2; exit 2 ;;
    esac
done

clone_or_update() {
    local url="$1" dest="$2" branch="${3:-}"
    if [ -d "$dest/.git" ]; then
        if [ "$UPDATE" = "1" ]; then
            echo "==> updating $dest"
            git -C "$dest" fetch -q origin
            if [ -n "$branch" ]; then
                git -C "$dest" checkout -q "$branch"
                git -C "$dest" pull -q --ff-only origin "$branch" || true
            else
                git -C "$dest" pull -q --ff-only || true
            fi
        else
            echo "==> already linked: $dest (use --update to refresh)"
        fi
    else
        echo "==> cloning $url -> $dest"
        if [ -n "$branch" ]; then
            git clone --depth 1 --branch "$branch" "$url" "$dest"
        else
            git clone --depth 1 "$url" "$dest"
        fi
    fi
}

mkdir -p "$LINKS"

# Public OHLCV datasets (Deriv synthetic indices + FP-Markets FX).
# The backtest loader assembles candles.csv.gz.part-* chunks automatically.
clone_or_update "https://github.com/abutronmall-netizen/market-data" \
    "$LINKS/market-data"

# Real settled-trade history (9 broker/account MT5 deal logs, 875 trades).
clone_or_update "https://github.com/abutronmall-netizen/abutron-pay" \
    "$LINKS/trade-history" "trade-history-2026-11-15"

if [ "$VENDOR" = "1" ]; then
    clone_or_update "https://github.com/abutronmall-netizen/abutron-apex" \
        "$ROOT/vendor/abutron-apex"
fi

echo "==> linked:"
echo "    ABUTRON_MARKET_DATA_DIR=$LINKS/market-data"
echo "    ABUTRON_TRADE_HISTORY_DIR=$LINKS/trade-history"
echo "==> verify:"
echo "    PYTHONPATH=src .venv/bin/python -m abutron.backtest.run --list"
echo "==> train (see docs/INSTALL_AND_LINK.md):"
echo "    PYTHONPATH=src .venv/bin/python -m scripts.train_unified"
echo "    PYTHONPATH=src .venv/bin/python -m scripts.train_online"
echo "    PYTHONPATH=src .venv/bin/python -m scripts.train_from_trades --mode online"
