#!/usr/bin/env bash
# Start everything the live demo needs, then open it in the browser.
#
#   scripts/start_demo.sh
#
# Starts (only if not already running), logs in .recordings/logs/:
#   - WebShop (official small setup) on 127.0.0.1:3000   (needs ../webshop)
#   - Codex model proxy on 127.0.0.1:18080                (needs `codex login` once)
#   - Live demo on http://127.0.0.1:8790
# Recipes live in MongoDB (MONGODB_URI in .env), so nothing is lost on restart.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WEBSHOP="${WEBSHOP_DIR:-$REPO/../webshop}"
LOGS="$REPO/.recordings/logs"
mkdir -p "$LOGS"

up() { curl -s -m 2 -o /dev/null "$1"; }

if up "http://127.0.0.1:3000/fixed_0" && curl -s -m 5 "http://127.0.0.1:3000/fixed_0" | grep -q Instruction; then
  echo "WebShop: already running"
elif [ -d "$WEBSHOP/.venv" ]; then
  echo "WebShop: starting"
  (cd "$WEBSHOP" && source webshop_env.sh && nohup .venv/bin/python -m web_agent_site.app --attrs >"$LOGS/webshop.log" 2>&1 &)
else
  echo "WebShop: not installed at $WEBSHOP (run scripts/setup_webshop.sh); Toolshop still works"
fi

if nc -z 127.0.0.1 18080 2>/dev/null; then
  echo "Model proxy: already running"
else
  echo "Model proxy: starting"
  (cd "$REPO" && nohup npx -y codex-as-api >"$LOGS/proxy.log" 2>&1 &)
fi

if up "http://127.0.0.1:8790/api/status"; then
  echo "Demo: already running"
else
  echo "Demo: starting"
  (cd "$REPO" && nohup uv run python demo/webshop_live.py >"$LOGS/demo.log" 2>&1 &)
fi

for _ in $(seq 1 60); do
  up "http://127.0.0.1:8790/api/status" && nc -z 127.0.0.1 18080 2>/dev/null && break
  sleep 1
done
# First WebShop request loads its data (a few seconds).
curl -s -m 120 -o /dev/null "http://127.0.0.1:3000/fixed_0" || true
curl -s "http://127.0.0.1:8790/api/status" | python3 -c \
  "import json,sys; s=json.load(sys.stdin); print('Status:', {k: s[k] for k in ('webshop', 'proxy', 'mongodb')})"
open "http://127.0.0.1:8790" 2>/dev/null || echo "Open http://127.0.0.1:8790"
