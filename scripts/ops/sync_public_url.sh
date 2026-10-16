#!/bin/bash
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT"
exec "$PROJECT/.venv/bin/python" -m reportagent.cli.sync_public_url "$@"
