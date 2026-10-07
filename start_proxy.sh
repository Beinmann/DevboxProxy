#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export PROXY_PORT="${PROXY_PORT:-8080}"
sudo docker network inspect devbox-net >/dev/null 2>&1 || sudo docker network create devbox-net >/dev/null
sudo env DEVBOX_PROXY_WRAPPER=1 PROXY_PORT="$PROXY_PORT" docker compose up -d --build
echo "==> DevBox proxy: http://localhost:$PROXY_PORT"
