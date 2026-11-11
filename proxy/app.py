"""DevBox port proxy.

UI at http://localhost:<port>/ lists running devbox containers. An app on
port P in container C is reachable at http://P.C.localhost:<port>/ — the
proxy routes on the Host header, so absolute asset paths and websockets
(HMR) work unchanged.

With a token set, the UI's /go mints a capability hostname
P.C.<secret>.localhost: the secret label is a signed, expiring HMAC, so
every request to that host is authorized without a cookie (sandboxed
iframes with an opaque origin can't send cookies).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from html import escape
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web

DOCKER_API = os.environ.get("DOCKER_API", "http://docker-socket-proxy:2375")
DEVBOX_IMAGE = os.environ.get("DEVBOX_IMAGE", "my-dev-box-v2")
# Shared network the proxy reaches boxes on (resolved by container name).
DEVBOX_NET = os.environ.get("DEVBOX_NET", "devbox-net")
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "localhost")
# Port the browser uses to reach us (the published host port), used when
# building links. Falls back to the port in the request's Host header.
PUBLIC_PORT = os.environ.get("PUBLIC_PORT", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
# host:port of the loopback connector; empty disables the
# fallback for servers bound only to 127.0.0.1 inside the container.
CONNECTOR = os.environ.get("CONNECTOR", "")

# Shared secret (ticket 012). Empty disables authentication. The UI is
# protected by a login cookie; proxied hosts by a capability label (below).
TOKEN = os.environ.get("PROXY_TOKEN", "")
COOKIE = "devbox_proxy_auth"
# A capability hostname minted by /go stays valid for between CAP_TTL and
# 2*CAP_TTL seconds (the expiry is rounded to a window so repeated Opens give
# the same hostname, and with it the same origin for localStorage etc.).
CAP_TTL = int(os.environ.get("CAP_TTL", str(12 * 3600)))
CAP_SIG_LEN = 32  # hex chars of HMAC kept: 128 bits
# Docker events: debounce before re-listing, and how long a quiet stream may
# stay silent before it is re-synced anyway (guards against a dead connection).
EVENT_DEBOUNCE = float(os.environ.get("EVENT_DEBOUNCE", "0.1"))
RESYNC_INTERVAL = float(os.environ.get("RESYNC_INTERVAL", "60"))
EVENT_RETRY = 1.0  # first wait before reconnecting the event stream; doubles up to 30 s
# Max wait for the upstream to connect / start answering (ticket 016): a
# stalled app or tunnel becomes a 502 instead of an endless spinner.
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "15"))
log = logging.getLogger("devbox-proxy")
# Send "localhost:<port>" as Host upstream (original in X-Forwarded-Host) for
# dev servers that reject unknown hosts (Vite/Astro "forbidden host").
REWRITE_HOST = os.environ.get("REWRITE_HOST", "") == "1"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# Handled by aiohttp itself on the websocket bridge.
WS_SKIP = HOP_BY_HOP | {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "host", "content-length",
}


async def _running(app, filters):
    url = f"{DOCKER_API}/containers/json"
    async with app["docker"].get(url, params={"filters": json.dumps(filters)}) as r:
        r.raise_for_status()
        return [c for c in await r.json() if c.get("Names")]


async def list_containers(app):
    """All running containers as dicts: name, devbox (created from the devbox
    image) and on_net (attached to DEVBOX_NET). Usable ones are those with both."""
    everything = await _running(app, {"status": ["running"]})
    devbox_ids = {c["Id"] for c in await _running(
        app, {"ancestor": [DEVBOX_IMAGE], "status": ["running"]})}
    return sorted(
        ({"name": c["Names"][0].lstrip("/"),
          "devbox": c["Id"] in devbox_ids,
          "on_net": DEVBOX_NET in (c.get("NetworkSettings") or {}).get("Networks", {})}
         for c in everything),
        key=lambda c: (not (c["devbox"] and c["on_net"]), c["name"]),
    )


async def list_devboxes(app):
    """Names of running containers the proxy can serve."""
    return [c["name"] for c in await list_containers(app) if c["devbox"] and c["on_net"]]


def split_host(host_header):
    host, _, port = host_header.partition(":")
    return host.lower(), port


# Dashed (192-168-2-227) is what we emit: nip.io resolves the *first* IP-like
# run in the name, so a container called "box-1" in "box-1.192.168.2.227.nip.io"
# would resolve to 1.192.168.2 (ticket 016). Dotted still accepted.
NIP_RE = re.compile(r"\.(\d{1,3}(\.\d{1,3}){3}|\d{1,3}(-\d{1,3}){3})\.nip\.io$")


def parse_target(host):
    """'3000.my-box.localhost' -> (3000, 'my-box', ''); None if not a proxy host.
    With a token set the last label before the domain is the capability:
    '3000.my-box.<cap>.localhost' -> (3000, 'my-box', '<cap>') (cap is None
    when absent). With the default BASE_DOMAIN, '3000.my-box.<ip>.nip.io'
    works too (what the UI links to when it was opened via an IP address)."""
    suffix = "." + BASE_DOMAIN
    if not host.endswith(suffix):
        m = NIP_RE.search(host) if BASE_DOMAIN == "localhost" else None
        if not m:
            return None
        suffix = m.group(0)
    labels = host[: -len(suffix)].split(".")
    if len(labels) < 2 or not labels[0].isdigit():
        return None
    port = int(labels[0])
    if not 1 <= port <= 65535:
        return None
    if not TOKEN:
        return port, ".".join(labels[1:]), ""
    if len(labels) < 3:
        return port, ".".join(labels[1:]), None
    return port, ".".join(labels[1:-1]), labels[-1]


def _sig(*parts):
    return hmac.new(TOKEN.encode(), ":".join(parts).encode(), hashlib.sha256).hexdigest()


def session_value(host):
    """Cookie value valid for exactly one host; cookies are host-only, so a
    box app on one subdomain can't read or replay another's."""
    return _sig("session", host)


def cap_label(port, container, now=None):
    """DNS label that authorizes requests to <port>.<container>.<label>.<domain>:
    10 hex chars expiry + HMAC over (port, container, expiry)."""
    exp = (int((now or time.time()) // CAP_TTL) + 2) * CAP_TTL
    return f"{exp:010x}" + _sig("cap", str(port), container, str(exp))[:CAP_SIG_LEN]


def cap_valid(port, container, label, now=None):
    if not TOKEN or not label or not re.fullmatch(r"[0-9a-f]{%d}" % (10 + CAP_SIG_LEN), label):
        return False
    exp = int(label[:10], 16)
    return (exp >= (now or time.time())
            and hmac.compare_digest(label[10:], _sig("cap", str(port), container, str(exp))[:CAP_SIG_LEN]))


def authenticated(request, host):
    """UI login: a host-only cookie."""
    if not TOKEN:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_value(host))


def set_session(resp, host):
    resp.set_cookie(COOKIE, session_value(host), httponly=True, samesite="Lax", path="/")


# Favicon: a box with an arrow passing through it. Inline so the single-file
# image needs no extra assets.
ICON_PATH = "/favicon.svg"
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1f2937"/>'
    '<path d="M16 5 26 10.5v11L16 27 6 21.5v-11z" fill="none" stroke="#60a5fa" '
    'stroke-width="2" stroke-linejoin="round"/>'
    '<path d="M3 16h26m-5-5 5 5-5 5" fill="none" stroke="#fbbf24" stroke-width="2.4" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>')


async def icon(request):
    return web.Response(text=ICON_SVG, content_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


LOGIN = """<!doctype html><meta charset=utf-8>
<title>DevBox proxy: login</title>
<link rel=icon type=image/svg+xml href=/favicon.svg>
<style>:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:24rem;margin:3rem auto;padding:0 1rem}.err{color:#c33}</style>
<h1>DevBox proxy</h1>%s
<form method=post action=/login>
<input type=password name=token placeholder="proxy token" autofocus required>
<button>Log in</button></form>"""


async def login(request):
    form = await request.post()
    given = str(form.get("token", ""))
    if not TOKEN or not hmac.compare_digest(_sig("login", given), _sig("login", TOKEN)):
        return web.Response(text=LOGIN % "<p class=err>Wrong token.</p>",
                            content_type="text/html", status=401)
    host, _ = split_host(request.headers.get("Host", ""))
    resp = web.HTTPSeeOther("/")
    set_session(resp, host)
    raise resp


PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>DevBox proxy</title>
<link rel=icon type=image/svg+xml href=/favicon.svg>
<style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}
body{{max-width:40rem;margin:2rem auto;padding:0 1rem}}
li{{margin:.6rem 0}} input{{width:6rem}}
.err,.bad{{color:#c33}} .bad small{{margin-left:.5rem}}
form.f{{margin:1rem 0}} form.f label{{display:block}}
</style>
<h1>DevBox containers</h1>{body}"""


async def index(request):
    hide_nondevbox = "hide_nondevbox" in request.query
    hide_nonet = "hide_nonet" in request.query
    try:
        containers = await list_containers(request.app)
    except Exception as e:  # docker socket proxy down, etc.
        body = f'<p class=err>Cannot reach Docker API: {escape(str(e))}</p>'
        return web.Response(text=PAGE.format(body=body), content_type="text/html", status=502)
    controls = (
        '<form class=f method=get>'
        f'<label><input type=checkbox name=hide_nondevbox value=1 onchange="this.form.submit()"'
        f'{" checked" if hide_nondevbox else ""}> hide non-devbox containers</label>'
        f'<label><input type=checkbox name=hide_nonet value=1 onchange="this.form.submit()"'
        f'{" checked" if hide_nonet else ""}> hide containers not on <code>{escape(DEVBOX_NET)}</code></label>'
        '<noscript><button>Apply</button></noscript></form>'
    )
    items = []
    for c in containers:
        reasons = []
        if not c["devbox"]:
            reasons.append(f"non-devbox image (not {DEVBOX_IMAGE})")
        if not c["on_net"]:
            reasons.append(f"not on {DEVBOX_NET}")
        if (hide_nondevbox and not c["devbox"]) or (hide_nonet and not c["on_net"]):
            continue
        n = escape(c["name"])
        if reasons:
            items.append(f'<li class=bad><b>{n}</b><small>{escape("; ".join(reasons))}</small></li>')
        else:
            items.append(
                f'<li><b>{n}</b> <form method=get action=/go style="display:inline">'
                f'<input type=hidden name=c value="{n}">'
                f'<input name=port type=number min=1 max=65535 placeholder=port required> '
                f'<button>Open</button></form></li>')
    body = controls + (f"<ul>{''.join(items)}</ul>" if items else "<p>No matching running containers.</p>")
    return web.Response(text=PAGE.format(body=body), content_type="text/html")


async def go(request):
    c = request.query.get("c", "")
    port = request.query.get("port", "")
    if not port.isdigit() or not c or not await request.app["devboxes"].allowed(c):
        raise web.HTTPBadRequest(text="unknown container or bad port")
    _, public = split_host(request.headers.get("Host", ""))
    public = PUBLIC_PORT or public
    suffix = f":{public}" if public else ""
    domain = BASE_DOMAIN
    ui_host, _ = split_host(request.headers.get("Host", ""))
    if domain == "localhost" and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ui_host):
        domain = f"{ui_host.replace('.', '-')}.nip.io"  # *.localhost wouldn't reach this machine
    # With a token the host itself is the credential: a signed, expiring label
    # that needs no cookie (cookies can't be shared across *.localhost, and
    # sandboxed iframes with an opaque origin can't send any).
    cap = f".{cap_label(int(port), c)}" if TOKEN else ""
    raise web.HTTPFound(f"http://{int(port)}.{quote(c)}{cap}.{domain}{suffix}/")


def forward_headers(request, skip, port=None):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    peer = request.transport.get_extra_info("peername")
    headers["X-Forwarded-For"] = peer[0] if peer else ""
    headers["X-Forwarded-Host"] = request.headers.get("Host", "")
    if REWRITE_HOST and port:
        headers["Host"] = f"localhost:{port}"
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


async def proxy(request, port, container):
    if not await request.app["devboxes"].allowed(container):
        return web.Response(status=404, text=f"No running devbox container named {container!r}.")
    upstream = f"{container}:{port}"
    path = request.raw_path
    # Host is left out of the log: it carries the capability label.
    log.info("%s %s -> %s", request.method, path, upstream)
    try:
        try:
            return await forward(request, upstream, path, port)
        except aiohttp.ClientConnectorError as e:
            # Nothing on the container's eth0: the server may be bound to
            # 127.0.0.1 only. Retry through the loopback connector.
            if not (CONNECTOR and isinstance(e.os_error, ConnectionRefusedError)):
                raise
            local = await tunnel_port(request.app, container, port)
            return await forward(request, f"127.0.0.1:{local}", path, port)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        log.warning("%s failed: %r", upstream, e)
        return web.Response(
            status=502,
            text=(f"Cannot reach {upstream}: {e or type(e).__name__}\n\n"
                  "Is something listening on that port? Is the proxy on the "
                  "container's network (devbox-net)? Servers bound to 127.0.0.1 "
                  "need the connector and `socat` in the devbox image."),
        )


async def forward(request, upstream, path, port):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request, upstream, path, port)
    return await proxy_http(request, upstream, path, port)


async def tunnel_port(app, container, port):
    """Local 127.0.0.1 port whose connections reach 127.0.0.1:<port> inside
    <container>, relayed through the connector. Created on first use."""
    key = (container, port)
    servers = app["tunnels"]
    if key not in servers:
        async def on_conn(r, w):
            host, _, cport = CONNECTOR.rpartition(":")
            try:
                async with asyncio.timeout(UPSTREAM_TIMEOUT):
                    ur, uw = await asyncio.open_connection(host, int(cport))
                    uw.write(f"{container} {port}\n".encode())
                    await uw.drain()
                    reply = (await ur.readline()).strip()
                if reply != b"OK":
                    raise ConnectionError(f"connector said {reply!r}")
            except (OSError, ConnectionError, TimeoutError) as e:
                log.warning("tunnel %s:%s failed: %r", container, port, e)
                w.close()
                return
            await asyncio.gather(relay(r, uw), relay(ur, w), return_exceptions=True)
            w.close()
            uw.close()
        servers[key] = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    return servers[key].sockets[0].getsockname()[1]


async def relay(src, dst):
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
        if dst.can_write_eof():
            dst.write_eof()
    except ConnectionError:
        pass


async def proxy_http(request, upstream, path, port):
    session = request.app["upstream"]
    has_body = request.can_read_body
    r = await asyncio.wait_for(session.request(
        request.method,
        f"http://{upstream}{path}",
        headers=forward_headers(request, HOP_BY_HOP, port),
        data=request.content if has_body else None,
        allow_redirects=False,
    ), UPSTREAM_TIMEOUT)
    async with r:
        resp = web.StreamResponse(status=r.status, reason=r.reason)
        for k, v in r.headers.items():
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                resp.headers.add(k, v)
        # Content-Length is kept for non-chunked bodies so browsers show progress.
        if "Content-Length" in r.headers and r.headers.get("Transfer-Encoding") is None:
            resp.content_length = int(r.headers["Content-Length"])
        await resp.prepare(request)
        async for chunk in r.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def proxy_ws(request, upstream, path, port):
    session = request.app["upstream"]
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    up = await asyncio.wait_for(session.ws_connect(
        f"http://{upstream}{path}",
        headers=forward_headers(request, WS_SKIP, port),
        protocols=protocols,
        max_msg_size=0,
        autoclose=False,
        autoping=False,
    ), UPSTREAM_TIMEOUT)
    async with up:
        down = web.WebSocketResponse(protocols=[up.protocol] if up.protocol else (), max_msg_size=0,
                                     autoclose=False, autoping=False)
        await down.prepare(request)

        async def pump(src, dst):
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.PING:
                    await dst.ping(msg.data)
                elif msg.type == aiohttp.WSMsgType.PONG:
                    await dst.pong(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
            if not dst.closed:
                await dst.close()

        tasks = [asyncio.ensure_future(pump(up, down)), asyncio.ensure_future(pump(down, up))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return down


@web.middleware
async def router(request, handler):
    host, _ = split_host(request.headers.get("Host", ""))
    target = parse_target(host)
    if target:
        port, container, cap = target
        if TOKEN and not cap_valid(port, container, cap):
            return web.Response(
                status=401,
                text="Unauthorized (missing, wrong or expired capability hostname). "
                     "Open the DevBox proxy UI, log in and use it to open this port.")
        return await proxy(request, port, container)
    if request.path == "/login" and request.method == "POST":
        return await handler(request)
    if request.path in (ICON_PATH, "/favicon.ico") and request.method == "GET":
        return await icon(request)
    if not authenticated(request, host):
        return web.Response(text=LOGIN % "", content_type="text/html", status=401)
    return await handler(request)


EVENT_FILTERS = {
    # What can change which containers are servable: start/stop, renames,
    # attaching to / detaching from the network, and image (re)tagging.
    "type": ["container", "network", "image"],
    "event": ["start", "die", "destroy", "rename", "pause", "unpause",
              "connect", "disconnect", "tag", "untag", "delete"],
}


class DevboxWatcher:
    """In-memory set of servable container names (running, devbox image, on
    DEVBOX_NET), kept current from Docker's event stream so proxied requests
    need no Docker API call. Any relevant event triggers a debounced re-list,
    so the set is always exactly what list_devboxes() returns: the allowlist
    rules live in one place and non-devbox containers stay blocked.

    The cache is only trusted while the event stream is up and the set is in
    sync with it; otherwise allowed() falls back to asking Docker directly.
    (Re)connecting re-lists and asks for events since just before it, so
    nothing that happened during an outage is missed."""

    def __init__(self, app):
        self.app = app
        self.names = frozenset()
        self.synced = False
        self.streaming = False
        self._gen = 0  # bumped per (re)connect; a sync from before it doesn't count
        self._dirty = asyncio.Event()
        self._tasks = []

    async def allowed(self, name):
        if self.synced and self.streaming:
            return name in self.names
        return name in await list_devboxes(self.app)

    async def sync(self):
        gen = self._gen
        names = frozenset(await list_devboxes(self.app))
        if gen == self._gen:
            self.names, self.synced = names, True

    async def start(self):
        try:
            await self.sync()
        except Exception as e:  # docker socket proxy may still be starting
            log.warning("initial container list failed: %r", e)
        self._tasks = [asyncio.create_task(self._sync_loop()), asyncio.create_task(self._stream_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _sync_loop(self):
        while True:
            await self._dirty.wait()
            await asyncio.sleep(EVENT_DEBOUNCE)  # coalesce bursts
            self._dirty.clear()
            try:
                await self.sync()
            except Exception as e:
                log.warning("container list failed: %r", e)
                self.synced = False
                await asyncio.sleep(2)
                self._dirty.set()

    async def _stream_loop(self):
        backoff = EVENT_RETRY
        while True:
            since = time.time() - 1
            self._gen += 1
            self.synced = False
            self._dirty.set()  # resync after every (re)connect
            try:
                async with self.app["docker"].get(
                    f"{DOCKER_API}/events",
                    params={"since": f"{since:.3f}", "filters": json.dumps(EVENT_FILTERS)},
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                ) as r:
                    r.raise_for_status()
                    self.streaming = True
                    backoff = EVENT_RETRY
                    while True:
                        try:
                            line = await asyncio.wait_for(r.content.readline(), RESYNC_INTERVAL)
                        except asyncio.TimeoutError:
                            line = b"\n"  # quiet: re-list anyway in case the connection died silently
                        if not line:
                            break
                        self._dirty.set()
            except (aiohttp.ClientError, OSError) as e:
                log.warning("docker event stream failed: %r", e)
            finally:
                self.streaming = False
                self.synced = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def on_response_prepare(request, response):
    # The capability hostname is a credential; keep it out of Referer headers
    # sent to other sites (the app's own subresources are same-origin anyway).
    response.headers["Referrer-Policy"] = "no-referrer"


async def on_startup(app):
    app["tunnels"] = {}
    app["docker"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    # No decompression/redirects: pass bytes through untouched.
    app["upstream"] = aiohttp.ClientSession(
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    app["devboxes"] = DevboxWatcher(app)
    await app["devboxes"].start()


async def on_cleanup(app):
    await app["devboxes"].stop()
    await app["docker"].close()
    await app["upstream"].close()
    for srv in app["tunnels"].values():
        srv.close()


def make_app():
    app = web.Application(middlewares=[router], client_max_size=0)
    app.router.add_get("/", index)
    app.router.add_get("/go", go)
    app.router.add_post("/login", login)
    app.router.add_get(ICON_PATH, icon)
    app.on_startup.append(on_startup)
    app.on_response_prepare.append(on_response_prepare)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        print("WARNING: PROXY_TOKEN is empty, authentication is disabled", flush=True)
    web.run_app(make_app(), host="0.0.0.0", port=LISTEN_PORT)
