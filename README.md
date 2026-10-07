# devbox-proxy

Sister repo to [DevBox](../devbox). One host port, a small web UI, and any
port of any running devbox container in your browser (ticket 004).

    ./start_proxy.sh        # builds + starts; UI at http://localhost:8080
    ./stop_proxy.sh

1. Open `http://localhost:8080`, pick a container, enter a port, **Open**.
2. You land on `http://<port>.<container>.localhost:8080/`, which is
   proxied to `<container>:<port>` (HTTP and websockets).

`PROXY_PORT=9000 ./start_proxy.sh` changes the host port.

## How it works

- **Host-based routing.** `*.localhost` resolves to 127.0.0.1 in Chrome and
  Firefox without any DNS setup (also from a Windows browser to a WSL
  proxy), so apps see a root path — absolute asset URLs and HMR work. Host
  is passed through unchanged (Vite allows `*.localhost` by default);
  `X-Forwarded-*` headers are added.
- **Reaching containers.** The proxy runs as a container on the shared
  Docker network `devbox-net`, which every devbox also joins (DevBox's
  compose file; the network is created by whichever script runs first).
  Targets are resolved by container name via Docker DNS, so no ports need
  publishing and it behaves the same on Linux, WSL and Docker Desktop.
  Requires a DevBox version with `devbox-net`; recreate older boxes
  (`./stop_dev_container.sh`, then start again).
- **Security.** Published on `127.0.0.1` only. The proxy only forwards to
  containers of image `my-dev-box-v2` (not an open proxy), and it gets
  Docker info via `tecnativa/docker-socket-proxy` restricted to
  `GET /containers`, never the raw socket.
- **Loopback-only servers.** If the direct connect is refused (app bound to
  `127.0.0.1` only), the proxy retries through the `connector` service,
  which runs `docker exec <box> socat - TCP:127.0.0.1:<port>` so the
  connection originates inside the container's network namespace. Needs
  `socat` in the devbox image (add it, then rebuild/recreate the box). The
  connector is the only component with the raw Docker socket; it is stdlib
  only, reachable just on the internal network, and only ever runs that
  one command in running containers of the devbox image.

## Known limitations

- Boxes share `devbox-net`, so they can reach each other too.
- HTTP only; see ticket 005 for a real domain + HTTPS.

## Tests

    python -m venv .venv && .venv/bin/pip install aiohttp
    .venv/bin/python -m unittest discover tests

Covers routing, HTTP/POST/redirects, websockets, the loopback connector
fallback and errors against a fake
Docker API. The Docker packaging itself (compose, scripts) is untested —
it was written in an environment without Docker.
