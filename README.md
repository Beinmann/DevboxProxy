# devbox-proxy

> **Deprecated.** This project is no longer the recommended way to reach
> devbox ports and is not being extended. It still works as described below.

Sister repo to [DevBox](https://github.com/Beinmann/DevBox). One host port, a small web UI, and any
port of any running devbox container in your browser.

    ./start_proxy.sh        # builds + starts; UI at http://localhost:4242
    ./stop_proxy.sh

1. Open `http://localhost:4242`, pick a container, enter a port, **Open**.
2. You land on `http://<port>.<container>.localhost:4242/`, which is
   proxied to `<container>:<port>` (HTTP and websockets).

`PROXY_PORT=9000 ./start_proxy.sh` changes the host port.

## Access token

Every request (UI and proxied hosts, websockets included) needs a shared
token. `start_proxy.sh` uses `$PROXY_TOKEN` if set, otherwise generates a
random one on first start and keeps it in `./.proxy_token` (mode 600,
gitignored); it prints only the first 4 characters. Retrieve it with
`cat .proxy_token`; rotate by deleting the file and restarting.

Open the UI, paste the token into the login form (an HttpOnly cookie on the
UI host). **Open** then redirects to a *capability hostname*,
`http://<port>.<container>.<secret>.localhost:4242/`, where `<secret>` is a
signed (HMAC with the token), expiring label bound to that port and
container. Every request to that host is authorized by the host alone, with
no cookie, so websockets work and so do sandboxed iframes with an opaque
origin (e.g. widget frames), which can't send cookies. The label is valid
for 12 h to 24 h (`CAP_TTL` seconds, default 43200; the expiry is rounded
so re-opening within a window gives the same hostname, which keeps
`localStorage` etc.); after that, use **Open** again. Rotating the token
invalidates all labels. Treat the hostname like a password: the proxy sends
`Referrer-Policy: no-referrer` on every response and leaves the host out of
its logs, but an app that sends its own `Origin` header (CORS, POST) to
another site, or whose URL you paste somewhere, still reveals it. Cookies
set by the apps are passed through untouched. `./start_proxy.sh --no-auth` disables the token (for testing
on a trusted machine; never combine with `--host` on a network you don't
fully control). An empty `PROXY_TOKEN` inside the container does the same
(warning at startup).

## How it works

- **Host-based routing.** `*.localhost` resolves to 127.0.0.1 in Chrome and
  Firefox without any DNS setup (also from a Windows browser to a WSL
  proxy), so apps see a root path — absolute asset URLs and HMR work. Host
  is passed through unchanged (Vite allows `*.localhost` by default);
  `X-Forwarded-*` headers are added.
- **Allowlist.** The set of servable containers (running, devbox image, on
  `devbox-net`) is held in memory and kept current from Docker's event
  stream (via the socket proxy's `GET /events`), so proxied requests make no
  Docker API call. Relevant events (start, die, rename, network
  connect/disconnect, image retag, ...) trigger a debounced re-list using
  the same rule as the UI, so non-devbox containers such as
  `docker-socket-proxy` stay blocked. It is filled on startup and re-listed
  after every (re)connect of the stream (asking for events since just before,
  so nothing is missed) and every 60 s of silence (`RESYNC_INTERVAL`). While
  the stream is down the proxy falls back to asking Docker per request.
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
  `${PROXY_BIND:-127.0.0.1}:${PROXY_PORT:-4242}`; only `--host` widens it,
  see below). Access to the UI and to ports is token-protected. The proxy only forwards to
  containers of image `my-dev-box-v2` (not an open proxy), and it gets
  Docker info via `tecnativa/docker-socket-proxy` restricted to
  `GET /containers` and `GET /events`, never the raw socket.
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

## LAN access (opt-in)

    ./start_proxy.sh --host        # or PROXY_HOST=1 ./start_proxy.sh
    BASE_DOMAIN=192-168-1-5.nip.io ./start_proxy.sh --host

Without the flag the proxy is published on `127.0.0.1` only. With it, it
binds `0.0.0.0` and the script prints a warning.

**Risks:** access is protected only by the login token (always on, see
above), and the traffic is plain HTTP, so the token and everything behind it
can be sniffed on the network. Anyone with the token can reach every port of
every devbox container — including dev servers, shells exposed over HTTP,
etc. Use only on a network you fully trust, or firewall the port.

**Hostnames:** `*.localhost` only resolves on the same machine, so links
from a remote client would break. Set `BASE_DOMAIN` to a wildcard domain
that resolves to the host (e.g. `<host-ip-with-dashes>.nip.io`, e.g. `192-168-1-5.nip.io`, or your own
`*.example.lan` DNS entry); links then become
`http://<port>.<container>.<BASE_DOMAIN>:<port>/`. Open the UI via a
name under that domain too, or via the IP.

If `BASE_DOMAIN` is left at its default and you open the UI via an IPv4
address, the Open links automatically use `<that-ip>.nip.io`.

## Dev servers that reject the hostname

Vite/Astro-style dev servers answer `forbidden host "…"` because the proxy
forwards the browser's `Host` header unchanged. Either allow the host in the
dev server (`server.allowedHosts`), or start the proxy with
`./start_proxy.sh --rewrite-host`: the app then sees `Host: localhost:<port>`
(the original goes in `X-Forwarded-Host`). Off by default, since apps that
build redirects or absolute URLs from `Host` would then point at `localhost`.

## Known limitations

- Boxes share `devbox-net`, so they can reach each other too.
- HTTP only, no HTTPS or real domain.
- Anyone holding a capability hostname can reach that port until it
  expires; with `--no-auth` the only protection is the loopback-only port
  binding, and any local process or website in your browser (via
  `*.localhost`) can reach the proxied ports.
- The `connector` service mounts the raw Docker socket as root, which is
  host-root-equivalent if compromised. See "Loopback-only servers" above for
  how its surface is limited.

## Tests

    python -m venv .venv && .venv/bin/pip install aiohttp
    .venv/bin/python -m unittest discover tests

Covers routing, HTTP/POST/redirects, websockets, the loopback connector
fallback, capability hostnames (valid, expired, tampered, wrong port or
container), the event-driven allowlist (updates, bursts, reconnect/resync,
fallback) and errors against a fake Docker API. The Docker packaging
(compose, scripts) is not covered by the tests.

## License

[MIT](LICENSE).
