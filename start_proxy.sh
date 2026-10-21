#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export PROXY_PORT="${PROXY_PORT:-8080}"
# Access token (ticket 012): PROXY_TOKEN from the environment, else the one in
# ./.proxy_token, else a fresh random one saved there (mode 600, gitignored).
if [ -z "${PROXY_TOKEN:-}" ]; then
  [ -s .proxy_token ] || (umask 077; head -c 24 /dev/urandom | base64 | tr -d '=+/\n' > .proxy_token)
  PROXY_TOKEN="$(cat .proxy_token)"
fi
export PROXY_TOKEN
sudo docker network inspect devbox-net >/dev/null 2>&1 || sudo docker network create devbox-net >/dev/null
sudo env DEVBOX_PROXY_WRAPPER=1 PROXY_PORT="$PROXY_PORT" PROXY_TOKEN="$PROXY_TOKEN" docker compose up -d --build
echo "==> DevBox proxy: http://localhost:$PROXY_PORT"
echo "==> Login token: ${PROXY_TOKEN:0:4}… (full value: cat .proxy_token)"
