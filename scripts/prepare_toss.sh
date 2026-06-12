#!/usr/bin/env bash
# Toss broker scaffolding — install tossctl + apply gates. Does NOT run auth login.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOSSCTL="${TOSSCTL_BIN:-$HOME/.local/bin/tossctl}"
# Project-local gates (safe default: allow_live_order_actions=false)
PROJECT_CONFIG="$ROOT/config/tossctl"
CONFIG_SRC="$PROJECT_CONFIG/config.json"
CONFIG_DST="${TOSSCTL_CONFIG_DIR:-$PROJECT_CONFIG}/config.json"

echo "== PBA-Toss: prepare tossctl (no auth / no live connect) =="

if ! command -v "$TOSSCTL" >/dev/null 2>&1; then
  echo "Building tossctl from source..."
  TMP=$(mktemp -d)
  git clone --depth 1 https://github.com/JungHoonGhae/tossinvest-cli.git "$TMP/tossinvest-cli"
  make -C "$TMP/tossinvest-cli" build
  mkdir -p "$(dirname "$TOSSCTL")"
  cp "$TMP/tossinvest-cli/bin/tossctl" "$TOSSCTL"
  chmod +x "$TOSSCTL"
  echo "Installed tossctl to $TOSSCTL"
fi

"$TOSSCTL" version

mkdir -p "$(dirname "$CONFIG_DST")"
if [[ -f "$CONFIG_SRC" ]]; then
  cp "$CONFIG_SRC" "$CONFIG_DST"
  chmod 600 "$CONFIG_DST" 2>/dev/null || true
  echo "Applied trading gates to $CONFIG_DST"
  echo "  allow_live_order_actions=false (kill switch ON)"
fi

echo ""
echo "Toss broker is READY but NOT connected."
echo "  Active broker: see config/settings.yaml (currently alpaca for paper)"
echo ""
echo "When switching to real Toss account later:"
echo "  1. bash scripts/install_tossctl_auth.sh"
echo "  2. tossctl auth login"
echo "  3. Set trading.broker: tossctl in config/settings.yaml"
echo "  4. bash scripts/enable_live_trading.sh  # only when ready for real orders"
echo "  5. PYTHONPATH=. python -m src.main status"
