#!/usr/bin/env bash
# Dedicated Python env for tossctl auth (fixes "playwright is required" on WSL).
set -euo pipefail

SHARE_DIR="$HOME/.local/share/tossctl"
AUTH_DIR="$SHARE_DIR/auth-helper"
VENV_DIR="$SHARE_DIR/auth-venv"
TMP="${TMPDIR:-/tmp}/tossinvest-cli-src"
TOSSCTL_BIN="${TOSSCTL_BIN:-$HOME/.local/bin/tossctl}"

resolve_python() {
  # 1) pyenv: use real interpreter path (avoids "python3.12: command not found" shim issue)
  if command -v pyenv >/dev/null 2>&1; then
    local pyenv_root ver
    pyenv_root="$(pyenv root)"
    for ver in 3.12.8 3.12.7 3.12 3.11.9 3.11; do
      if [[ -x "${pyenv_root}/versions/${ver}/bin/python" ]]; then
        echo "${pyenv_root}/versions/${ver}/bin/python"
        return 0
      fi
    done
    ver="$(pyenv versions --bare 2>/dev/null | grep -E '^3\.(11|12)' | tail -1 || true)"
    if [[ -n "$ver" && -x "${pyenv_root}/versions/${ver}/bin/python" ]]; then
      echo "${pyenv_root}/versions/${ver}/bin/python"
      return 0
    fi
  fi

  # 2) explicit versioned binaries
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
        command -v "$candidate"
        return 0
      fi
    fi
  done

  return 1
}

echo "== tossctl auth environment (WSL-friendly) =="

PY="$(resolve_python)" || {
  echo "Python 3.11+ required."
  echo "  pyenv: pyenv install 3.12.8"
  echo "  apt:   sudo apt install python3.12-venv"
  exit 1
}
echo "Bootstrap Python: $PY ($("$PY" --version))"

# auth-helper source
if [[ ! -d "$AUTH_DIR" ]]; then
  [[ -d "$TMP/.git" ]] || git clone --depth 1 https://github.com/JungHoonGhae/tossinvest-cli.git "$TMP"
  mkdir -p "$SHARE_DIR"
  cp -a "$TMP/auth-helper" "$AUTH_DIR"
fi

# isolated venv — tossctl always uses this via env var
rm -rf "$VENV_DIR"
"$PY" -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
pip install -q -e "$AUTH_DIR"
pip install -q playwright
python -m playwright install chrome 2>/dev/null || python -m playwright install chromium

ENV_SNIPPET="$SHARE_DIR/auth.env"
cat > "$ENV_SNIPPET" <<EOF
# Source before tossctl auth:  source ~/.local/share/tossctl/auth.env
export TOSSCTL_AUTH_HELPER_PYTHON="$VENV_DIR/bin/python"
export TOSSCTL_AUTH_HELPER_DIR="$AUTH_DIR"
EOF

echo ""
echo "Installed auth venv: $VENV_DIR"
echo "Wrote: $ENV_SNIPPET"
echo ""
echo "Verify:"
TOSSCTL_AUTH_HELPER_PYTHON="$VENV_DIR/bin/python" "$TOSSCTL_BIN" doctor 2>&1 | grep -E 'python_binary|playwright|chrome|auth_helper' || true

echo ""
echo "Login (WSL headless):"
echo "  bash scripts/tossctl_auth_login.sh"
echo "  # or:"
echo "  source $ENV_SNIPPET"
echo "  tossctl auth login --headless --qr-output /tmp/toss-qr.png"
