#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

sudo env DEVBOX_PROXY_WRAPPER=1 docker compose down
