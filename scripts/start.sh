#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../m0"
PORT="${DIRECTOR_PORT:-8666}"
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$PORT < 1024 || 10#$PORT > 65535)); then
  echo 'DIRECTOR_PORT must be a port between 1024 and 65535.' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo 'Create m0/.venv and install m0/requirements.txt first. See README.md.' >&2
  exit 1
fi
umask 077
exec .venv/bin/python -m uvicorn webapp.server:app --host 127.0.0.1 --port "$PORT" --workers 1 --no-proxy-headers --no-access-log
