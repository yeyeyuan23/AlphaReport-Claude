#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
PROJECT="$(pwd)"
exec "$PROJECT/.venv/bin/python" -m reportagent.bot.feishu "$@"
