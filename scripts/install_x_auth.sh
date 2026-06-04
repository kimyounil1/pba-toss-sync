#!/usr/bin/env bash
# Ensure Playwright + Chrome for X browser session (WSL-friendly).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "Run scripts/setup.sh first to create .venv"
  exit 1
fi

pip install -q playwright
python -m playwright install chrome 2>&1 | grep -v 'already installed' || true

echo ""
echo "브라우저 설치 완료 (위 'already installed' 메시지는 정상입니다)."
echo "다음 단계 — X 로그인 (세션 파일 생성):"
echo "  bash scripts/x_auth_login.sh"
echo ""
echo "로그인 후 확인:"
echo "  PYTHONPATH=. python -m src.main x-auth-status"
