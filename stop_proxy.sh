#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Detach from devbox networks first, otherwise Docker refuses to remove them
# later (e.g. when ./stop_dev_container.sh runs `docker compose down`).
for net in $(sudo docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' devbox-proxy 2>/dev/null || true); do
  case "$net" in *devbox-proxy*) ;; *) sudo docker network disconnect "$net" devbox-proxy || true ;; esac
done
sudo env DEVBOX_PROXY_WRAPPER=1 docker compose down
