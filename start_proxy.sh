#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Loopback only by default. --host (or PROXY_HOST=1) publishes on all
# interfaces so other machines can reach it.
for arg in "$@"; do
  case "$arg" in
    --host) PROXY_HOST=1 ;;
    --rewrite-host) REWRITE_HOST=1 ;;
    --no-auth) NO_AUTH=1 ;;
    *) echo "usage: $0 [--host] [--rewrite-host] [--no-auth]" >&2; exit 2 ;;
  esac
done

export PROXY_PORT="${PROXY_PORT:-4242}"
# Access token: PROXY_TOKEN from the environment, else the one in
# ./.proxy_token, else a fresh random one saved there (mode 600, gitignored).
if [ "${NO_AUTH:-0}" = 1 ]; then
  PROXY_TOKEN=""
elif [ -z "${PROXY_TOKEN:-}" ]; then
  [ -s .proxy_token ] || (umask 077; head -c 24 /dev/urandom | base64 | tr -d '=+/\n' > .proxy_token)
  PROXY_TOKEN="$(cat .proxy_token)"
fi
export PROXY_TOKEN
export BASE_DOMAIN="${BASE_DOMAIN:-localhost}"
export REWRITE_HOST="${REWRITE_HOST:-}"
if [ "${PROXY_HOST:-0}" = 1 ]; then
  PROXY_BIND=0.0.0.0
  echo "!!! WARNING: LAN mode. The proxy is reachable from your network on port $PROXY_PORT" >&2
  echo "!!! and forwards to EVERY port of EVERY devbox container," >&2
  echo "!!! protected only by the login token, over plain HTTP." >&2
  echo "!!! Only use this on a network you fully trust." >&2
  if [ "$BASE_DOMAIN" = localhost ]; then
    echo "!!! Links use *.localhost, which does not resolve on other machines. Set" >&2
    echo "!!! BASE_DOMAIN to a wildcard domain pointing at this host, e.g." >&2
    echo "!!! BASE_DOMAIN=192-168-1-5.nip.io (see README)." >&2
  fi
else
  PROXY_BIND=127.0.0.1
fi
export PROXY_BIND

sudo docker network inspect devbox-net >/dev/null 2>&1 || sudo docker network create devbox-net >/dev/null
sudo env DEVBOX_PROXY_WRAPPER=1 PROXY_PORT="$PROXY_PORT" PROXY_BIND="$PROXY_BIND" PROXY_TOKEN="$PROXY_TOKEN" BASE_DOMAIN="$BASE_DOMAIN" REWRITE_HOST="$REWRITE_HOST" docker compose up -d --build
if [ "$PROXY_BIND" = 127.0.0.1 ]; then
  echo "==> DevBox proxy: http://localhost:$PROXY_PORT"
else
  echo "==> DevBox proxy (LAN): http://<this-host>:$PROXY_PORT"
fi
if [ "${NO_AUTH:-0}" = 1 ]; then
  echo "!!! --no-auth: login token DISABLED, anyone who can reach the port has full access." >&2
else
  echo "==> Login token: ${PROXY_TOKEN:0:4}… (full value: cat .proxy_token)"
fi
