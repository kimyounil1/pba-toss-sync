#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOSSCTL="${TOSSCTL_BIN:-$HOME/.local/bin/tossctl}"
CONFIG_SRC="$ROOT/config/toss_gates.json"
CONFIG_DST="${TOSSCTL_CONFIG_DIR:-$HOME/.config/tossctl}/config.json"

echo "== PBA-Toss Sync Setup =="

# 1. Build/install tossctl if missing
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

# 2. Python venv (3.12+)
cd "$ROOT"
PY="${PYTHON:-python3.12}"
if ! command -v "$PY" >/dev/null 2>&1; then
  if command -v pyenv >/dev/null 2>&1; then
    export PYENV_VERSION="${PYENV_VERSION:-3.12.8}"
    PY=python
  else
    PY=python3
  fi
fi
rm -rf .venv
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pytest pytest-asyncio
pip install -q google-genai 2>/dev/null || pip install -q google-generativeai
bash "$ROOT/scripts/install_x_auth.sh" || echo "Warning: playwright/chrome install failed — run scripts/install_x_auth.sh"

# 3. Apply trading gates template
mkdir -p "$(dirname "$CONFIG_DST")"
if [[ -f "$CONFIG_SRC" ]]; then
  cp "$CONFIG_SRC" "$CONFIG_DST"
  echo "Applied trading gates to $CONFIG_DST"
  echo "  allow_live_order_actions=false (dry-run safe default)"
fi

# 4. Auth check
echo ""
echo "Auth status:"
"$TOSSCTL" auth status || true

echo ""
echo "Install auth-helper (required for tossctl auth login):"
bash "$ROOT/scripts/install_tossctl_auth.sh" || echo "Warning: auth-helper install failed — run scripts/install_tossctl_auth.sh manually"
echo "  1. cp .env.example .env  # GEMINI_API_KEY or vLLM in .env"
echo "  2. bash scripts/x_auth_import.sh  # WSL: Windows Chrome 쿠키 import"
echo "     (또는 GUI 있으면: bash scripts/x_auth_login.sh)"
echo "  3. tossctl auth login    # QR scan + '이 기기 로그인 유지'"
echo "  4. Edit config/settings.yaml — set pba.username"
echo "  5. PYTHONPATH=. python -m src.main status"
echo "  6. PYTHONPATH=. python -m src.main daemon   # dry-run by default"
echo ""
echo "Enable live trading (Phase 2+):"
echo "  bash scripts/enable_live_trading.sh"
echo "  Set trading.dry_run: false in config/settings.yaml"
