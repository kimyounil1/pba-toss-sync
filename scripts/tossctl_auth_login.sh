#!/usr/bin/env bash
# WSL-friendly tossctl login wrapper.
set -euo pipefail

AUTH_ENV="${HOME}/.local/share/tossctl/auth.env"
if [[ ! -f "$AUTH_ENV" ]]; then
  echo "Run first: bash scripts/install_tossctl_auth.sh"
  exit 1
fi
# shellcheck disable=SC1090
source "$AUTH_ENV"

exec tossctl auth login --headless --qr-output /tmp/toss-qr.png "$@"
