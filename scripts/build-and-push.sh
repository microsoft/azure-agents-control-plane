#!/bin/bash
# Shared gated build; never opens ACR or changes firewall/bypass settings.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [ -n "${DEPLOYMENT_PYTHON:-}" ]; then
    PYTHON="$DEPLOYMENT_PYTHON"
elif [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    PYTHON=python
fi
options=(--from-azd)
if [ -n "${AZURE_ENV_NAME:-}" ]; then options+=("--environment=$AZURE_ENV_NAME"); fi
exec "$PYTHON" "$ROOT/scripts/build_and_push.py" "${options[@]}" "$@"
