#!/usr/bin/env bash
# X login via Playwright Chrome window (needs GUI / WSLg)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STORAGE="${X_SESSION_FILE:-$HOME/.config/pba-toss-sync/x-storage-state.json}"

if [[ ! -f "$ROOT/.venv/bin/activate" ]]; then
  echo "먼저 실행: bash scripts/setup.sh"
  exit 1
fi

if [[ -z "${DISPLAY:-}" ]] && [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo ""
  echo "WSL 터미널(CLI)에는 브라우저 창이 뜨지 않습니다."
  echo "tossctl처럼 QR headless 로그인은 X에서 지원하지 않습니다."
  echo ""
  echo "대신 쿠키 import (Windows Chrome → WSL):"
  echo "  bash scripts/x_auth_import.sh"
  echo ""
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo ""
echo "※ GUI가 있는 환경에서만 동작합니다. WSL에서 창이 안 보이면:"
echo "   bash scripts/x_auth_import.sh"
echo ""

python -m x_auth_helper login --storage-state "$STORAGE" --url "https://x.com/login"

if [[ ! -f "$STORAGE" ]]; then
  echo ""
  echo "오류: 세션 파일 없음 → $STORAGE"
  echo "WSL이면: bash scripts/x_auth_import.sh"
  exit 1
fi

echo ""
PYTHONPATH="$ROOT" python -m src.main x-auth-status
