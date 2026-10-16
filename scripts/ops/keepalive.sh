#!/bin/bash
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$PROJECT/data"
PID_FILE="$PROJECT/data/alphareport.pid"
if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then exit 0; fi
fi
nohup bash "$PROJECT/start.sh" >> "$PROJECT/data/bot.log" 2>&1 &
