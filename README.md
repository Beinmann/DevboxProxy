# devbox-proxy

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

Open the UI, paste the token into the login form. **Open** then redirects to
the app host via a signed link valid for 60 s that sets a cookie for that
one host (`*.localhost` can't share a cookie across subdomains), so
websockets work too. The cookie is HttpOnly, host-bound, and not forwarded
to the apps. `./start_proxy.sh --no-auth` disables the token (for testing
on a trusted machine; never combine with `--host` on a network you don't
fully control). An empty `PROXY_TOKEN` inside the container does the same
(warning at startup).

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
  `${PROXY_BIND:-127.0.0.1}:${PROXY_PORT:-4242}`; only `--host` widens it,
  see below). Access to the UI and to ports is token-protected. The proxy only forwards to
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
