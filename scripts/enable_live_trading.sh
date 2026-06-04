#!/usr/bin/env bash
# Enable live order execution in tossctl config (Phase 2+).
set -euo pipefail

CONFIG="${TOSSCTL_CONFIG_DIR:-$HOME/.config/tossctl}/config.json"

if [[ ! -f "$CONFIG" ]]; then
  echo "Config not found: $CONFIG — run scripts/setup.sh first"
  exit 1
fi

python3 - <<'PY'
import json, os
path = os.path.expanduser(os.environ.get("TOSSCTL_CONFIG_DIR", "~/.config/tossctl") + "/config.json")
with open(path) as f:
    cfg = json.load(f)
t = cfg.setdefault("trading", {})
t["place"] = True
t["sell"] = True
t["fractional"] = True
t["cancel"] = True
t["allow_live_order_actions"] = True
t.setdefault("dangerous_automation", {})["accept_fx_consent"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("Live trading enabled in", path)
print("Also set trading.dry_run: false in config/settings.yaml")
PY
