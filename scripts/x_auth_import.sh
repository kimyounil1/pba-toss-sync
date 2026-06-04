#!/usr/bin/env bash
# WSL/CLI용 X 세션: Windows Chrome에서 복사한 쿠키로 로그인 (브라우저 창 불필요)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STORAGE="${X_SESSION_FILE:-$HOME/.config/pba-toss-sync/x-storage-state.json}"
COOKIES_FILE="${X_COOKIES_FILE:-$HOME/.config/pba-toss-sync/x-cookies.env}"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo ""
echo "=== WSL용 X 세션 (쿠키 import) ==="
echo ""
echo "Windows Chrome에서 x.com 로그인(구독 계정) 후:"
echo "  F12 → Application → Cookies → https://x.com"
echo "  • auth_token  값 복사"
echo "  • ct0         값 복사"
echo ""
echo "방법 A) 파일에 저장 (권장):"
echo "  nano $COOKIES_FILE"
echo "  내용:"
echo "    X_AUTH_TOKEN=여기에_붙여넣기"
echo "    X_CT0=여기에_붙여넣기"
echo "  chmod 600 $COOKIES_FILE"
echo ""
echo "방법 B) 아래에 직접 입력 (입력 내용은 화면에 안 보임)"
echo ""

if [[ -f "$COOKIES_FILE" ]]; then
  echo "쿠키 파일 사용: $COOKIES_FILE"
  python -m x_auth_helper import-cookies \
    --storage-state "$STORAGE" \
    --cookies-file "$COOKIES_FILE" \
    --verify
else
  read -r -p "auth_token: " AUTH_TOKEN
  read -r -s -p "ct0: " CT0
  echo ""
  python -m x_auth_helper import-cookies \
    --storage-state "$STORAGE" \
    --auth-token "$AUTH_TOKEN" \
    --ct0 "$CT0" \
    --verify
fi

echo ""
PYTHONPATH="$ROOT" python -m src.main x-auth-status
