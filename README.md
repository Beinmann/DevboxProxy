# devbox-proxy

Sister repo to [DevBox](https://github.com/Beinmann/DevBox). One host port, a small web UI, and any
port of any running devbox container in your browser.

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
  (`./stop_dev_container.sh`, then start again). Boxes must also use the
  devbox image `my-dev-box-v2` (see Security).
- **UI.** Lists all running containers; ones not from the devbox image or
  not on `devbox-net` are red with the reason and can't be opened. Two
  checkboxes hide those rows. (`DEVBOX_NET` overrides the network name.)
- **Security.** Published on `127.0.0.1` only (compose binds
  `127.0.0.1:${PROXY_PORT:-8080}`; don't widen it). Requests that look
  cross-site are rejected with 403: `Sec-Fetch-Site: cross-site`, or an
  `Origin` that isn't the UI/a proxy host on the same port — so other
  websites can't reach boxes via `*.localhost`. Requests with neither header
  (curl, old browsers) are allowed. The proxy only forwards to
  containers of image `my-dev-box-v2` (not an open proxy), and it gets
  Docker info via `tecnativa/docker-socket-proxy` restricted to
  `GET /containers`, never the raw socket.
- **Image name.** `my-dev-box-v2` is the image name built by
  [DevBox](https://github.com/Beinmann/DevBox)'s scripts and compose file.
  Set `DEVBOX_IMAGE` to override it if your DevBox image is named
  differently.
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
- HTTP only, no HTTPS or real domain.
- No authentication: the only protection is the loopback-only port binding.
  Any local process, and any website open in your browser (via
  `*.localhost`), can reach the proxied ports.
- The `connector` service mounts the raw Docker socket as root, which is
  host-root-equivalent if compromised. See "Loopback-only servers" above for
  how its surface is limited.

## Tests

    python -m venv .venv && .venv/bin/pip install aiohttp
    .venv/bin/python -m unittest discover tests

Covers routing, HTTP/POST/redirects, websockets, the loopback connector
fallback and errors against a fake Docker API. The Docker packaging
(compose, scripts) is not covered by the tests.

## License

[MIT](LICENSE).
