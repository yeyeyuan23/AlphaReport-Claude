#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
PYTHON="${ALPHAREPORT_PYTHON:-$PROJECT/.venv/bin/python}"
PORT="${ALPHAREPORT_PORT:-8000}"
BIND_HOST="${ALPHAREPORT_BIND_HOST:-127.0.0.1}"
SERVER_PID=""
FEISHU_PID=""
TUNNEL_PID=""
PID_FILE="$PROJECT/data/alphareport.pid"
if [[ ! -x "$PYTHON" ]]; then
    echo "Missing .venv. Create a Python 3.10+ environment and install requirements/runtime.txt." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import fastapi, uvicorn, lark_oapi, anthropic' >/dev/null 2>&1; then
    echo "Missing dependencies. Run: .venv/bin/python -m pip install -r requirements/runtime.txt" >&2
    exit 1
fi
if [[ "${1:-}" == "--check" ]]; then
    echo "Local server and bot dependencies are available."
    exit 0
fi
if [[ ! -f .env && ( -z "${FEISHU_APP_ID:-}" || -z "${FEISHU_APP_SECRET:-}" ) ]]; then
    echo "Missing .env. Copy .env.example and configure your credentials and allowed sender IDs." >&2
    exit 1
fi
if [[ "${ALPHAREPORT_DEPLOYMENT:-false}" == "true" ]]; then
    "$PYTHON" -m reportagent.cli.check_deployment
fi
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is occupied. Stop its owner or select ALPHAREPORT_PORT; no process was stopped." >&2
    exit 1
fi
if [[ "${ALPHAREPORT_FRONTEND_PREBUILT:-false}" == "true" ]]; then
    if [[ ! -f frontend/dist/index.html ]]; then
        echo "Missing prebuilt frontend/dist/index.html; rebuild the deployment image." >&2
        exit 1
    fi
else
    if [[ ! -d frontend/node_modules ]]; then
        echo "Install frontend dependencies first: cd frontend && npm ci" >&2
        exit 1
    fi
    (cd frontend && npm run build)
fi
mkdir -p data
cleanup() {
    trap - EXIT INT TERM
    for pid in "$SERVER_PID" "$FEISHU_PID" "$TUNNEL_PID"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    done
    for pid in "$SERVER_PID" "$FEISHU_PID" "$TUNNEL_PID"; do
        if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
    done
    rm -f "$PID_FILE"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "$$" > "$PID_FILE"
"$PYTHON" -m uvicorn reportagent.main:app --host "$BIND_HOST" --port "$PORT" &
SERVER_PID=$!
"$PYTHON" -u -m reportagent.bot.feishu &
FEISHU_PID=$!
if [[ "${ALPHAREPORT_PUBLIC_TUNNEL:-false}" == "true" ]]; then
    command -v cloudflared >/dev/null || { echo "cloudflared is not installed" >&2; exit 1; }
    cloudflared tunnel --url "http://127.0.0.1:$PORT" > "$PROJECT/data/cloudflared.log" 2>&1 &
    TUNNEL_PID=$!
fi
echo "AlphaReport: http://127.0.0.1:$PORT — Ctrl+C to stop"
while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$FEISHU_PID" 2>/dev/null; do
    sleep 1
done
echo "A required service stopped; shutting down the other services." >&2
exit 1
