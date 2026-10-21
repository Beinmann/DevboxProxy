#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Loopback only by default. --lan (or PROXY_LAN=1) publishes on all
# interfaces so other machines can reach it (ticket 013).
for arg in "$@"; do
  case "$arg" in
    --lan) PROXY_LAN=1 ;;
    *) echo "usage: $0 [--lan]" >&2; exit 2 ;;
  esac
done

export PROXY_PORT="${PROXY_PORT:-8080}"
export BASE_DOMAIN="${BASE_DOMAIN:-localhost}"
if [ "${PROXY_LAN:-0}" = 1 ]; then
  PROXY_BIND=0.0.0.0
  echo "!!! WARNING: LAN mode. The proxy is reachable from your network on port $PROXY_PORT" >&2
  echo "!!! and forwards to EVERY port of EVERY devbox container, with NO authentication" >&2
  echo "!!! (password/token protection, ticket 012, is not available yet)." >&2
  echo "!!! Only use this on a network you fully trust." >&2
  if [ "$BASE_DOMAIN" = localhost ]; then
    echo "!!! Links use *.localhost, which does not resolve on other machines. Set" >&2
    echo "!!! BASE_DOMAIN to a wildcard domain pointing at this host, e.g." >&2
    echo "!!! BASE_DOMAIN=192.168.1.5.nip.io (see README)." >&2
  fi
else
  PROXY_BIND=127.0.0.1
fi
export PROXY_BIND

sudo docker network inspect devbox-net >/dev/null 2>&1 || sudo docker network create devbox-net >/dev/null
sudo env DEVBOX_PROXY_WRAPPER=1 PROXY_PORT="$PROXY_PORT" PROXY_BIND="$PROXY_BIND" BASE_DOMAIN="$BASE_DOMAIN" docker compose up -d --build
if [ "$PROXY_BIND" = 127.0.0.1 ]; then
  echo "==> DevBox proxy: http://localhost:$PROXY_PORT"
else
  echo "==> DevBox proxy (LAN): http://<this-host>:$PROXY_PORT"
fi
