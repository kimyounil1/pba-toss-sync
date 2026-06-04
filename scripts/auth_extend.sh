#!/usr/bin/env bash
# Extend tossctl session via phone push approval. Run daily via cron.
set -euo pipefail
TOSSCTL="${TOSSCTL_BIN:-$HOME/.local/bin/tossctl}"
"$TOSSCTL" auth extend --timeout 120s
