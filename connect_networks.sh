#!/bin/bash
# Attach the proxy container to the Docker network(s) of every running devbox
# container. Idempotent; re-run after starting a new devbox.
set -euo pipefail

IMAGE="${DEVBOX_IMAGE:-my-dev-box-v2}"
PROXY=devbox-proxy

for c in $(sudo docker ps --filter "ancestor=$IMAGE" --format '{{.Names}}'); do
  for net in $(sudo docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c"); do
    if sudo docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PROXY" | grep -qw -- "$net"; then
      continue
    fi
    echo "==> Connecting $PROXY to $net (for $c)"
    sudo docker network connect "$net" "$PROXY"
  done
done
