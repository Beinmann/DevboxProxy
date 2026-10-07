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
- **Reaching containers.** The proxy runs as a container and is attached to
  each devbox's compose network by `connect_networks.sh` (run by
  `start_proxy.sh`; re-run after starting a new devbox). Targets are
  resolved by container name via Docker DNS, so no ports need publishing
  and it behaves the same on Linux, WSL and Docker Desktop.
- **Security.** Published on `127.0.0.1` only. The proxy only forwards to
  containers of image `my-dev-box-v2` (not an open proxy), and it gets
  Docker info via `tecnativa/docker-socket-proxy` restricted to
  `GET /containers`, never the raw socket.
- The app in the container must listen on `0.0.0.0`, not just `127.0.0.1`.

## Known limitations

- A devbox started after the proxy needs `./connect_networks.sh`.
- While attached, the proxy keeps a devbox's compose network "in use", so
  that devbox's `docker compose down` can't remove the network. Run
  `./stop_proxy.sh` first (it detaches). A shared external network in
  DevBox's compose file would fix both; not done yet.
- HTTP only; see ticket 005 for a real domain + HTTPS.

## Tests

    python -m venv .venv && .venv/bin/pip install aiohttp
    .venv/bin/python -m unittest discover tests

Covers routing, HTTP/POST/redirects, websockets and errors against a fake
Docker API. The Docker packaging itself (compose, scripts) is untested —
it was written in an environment without Docker.
