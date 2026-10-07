"""DevBox port proxy.

UI at http://localhost:<port>/ lists running devbox containers. An app on
port P in container C is reachable at http://P.C.localhost:<port>/ — the
proxy routes on the Host header, so absolute asset paths and websockets
(HMR) work unchanged.
"""
import asyncio
import json
import os
from html import escape
from urllib.parse import quote

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
# host:port of the loopback connector (ticket 006); empty disables the
# fallback for servers bound only to 127.0.0.1 inside the container.
CONNECTOR = os.environ.get("CONNECTOR", "")

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


def parse_target(host):
    """'3000.my-box.localhost' -> (3000, 'my-box'); None if not a proxy host."""
    suffix = "." + BASE_DOMAIN
    if not host.endswith(suffix):
        return None
    labels = host[: -len(suffix)].split(".")
    if len(labels) < 2 or not labels[0].isdigit():
        return None
    port = int(labels[0])
    if not 1 <= port <= 65535:
        return None
    return port, ".".join(labels[1:])


PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>DevBox proxy</title>
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
    if not port.isdigit() or not c or c not in await list_devboxes(request.app):
        raise web.HTTPBadRequest(text="unknown container or bad port")
    _, public = split_host(request.headers.get("Host", ""))
    public = PUBLIC_PORT or public
    suffix = f":{public}" if public else ""
    raise web.HTTPFound(f"http://{int(port)}.{quote(c)}.{BASE_DOMAIN}{suffix}/")


def forward_headers(request, skip):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    peer = request.transport.get_extra_info("peername")
    headers["X-Forwarded-For"] = peer[0] if peer else ""
    headers["X-Forwarded-Host"] = request.headers.get("Host", "")
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


async def proxy(request, port, container):
    if container not in await list_devboxes(request.app):
        return web.Response(status=404, text=f"No running devbox container named {container!r}.")
    upstream = f"{container}:{port}"
    path = request.raw_path
    try:
        try:
            return await forward(request, upstream, path)
        except aiohttp.ClientConnectorError as e:
            # Nothing on the container's eth0: the server may be bound to
            # 127.0.0.1 only. Retry through the loopback connector.
            if not (CONNECTOR and isinstance(e.os_error, ConnectionRefusedError)):
                raise
            local = await tunnel_port(request.app, container, port)
            return await forward(request, f"127.0.0.1:{local}", path)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        return web.Response(
            status=502,
            text=(f"Cannot reach {upstream}: {e}\n\n"
                  "Is something listening on that port? Is the proxy on the "
                  "container's network (devbox-net)? Servers bound to 127.0.0.1 "
                  "need the connector and `socat` in the devbox image."),
        )


async def forward(request, upstream, path):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request, upstream, path)
    return await proxy_http(request, upstream, path)


async def tunnel_port(app, container, port):
    """Local 127.0.0.1 port whose connections reach 127.0.0.1:<port> inside
    <container>, relayed through the connector. Created on first use."""
    key = (container, port)
    servers = app["tunnels"]
    if key not in servers:
        async def on_conn(r, w):
            host, _, cport = CONNECTOR.rpartition(":")
            try:
                ur, uw = await asyncio.open_connection(host, int(cport))
                uw.write(f"{container} {port}\n".encode())
                await uw.drain()
                if (await ur.readline()).strip() != b"OK":
                    raise ConnectionError("connector refused")
            except (OSError, ConnectionError):
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


async def proxy_http(request, upstream, path):
    session = request.app["upstream"]
    has_body = request.can_read_body
    async with session.request(
        request.method,
        f"http://{upstream}{path}",
        headers=forward_headers(request, HOP_BY_HOP),
        data=request.content if has_body else None,
        allow_redirects=False,
    ) as r:
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


async def proxy_ws(request, upstream, path):
    session = request.app["upstream"]
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    async with session.ws_connect(
        f"http://{upstream}{path}",
        headers=forward_headers(request, WS_SKIP),
        protocols=protocols,
        max_msg_size=0,
        autoclose=False,
        autoping=False,
    ) as up:
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
        return await proxy(request, *target)
    return await handler(request)


async def on_startup(app):
    app["tunnels"] = {}
    app["docker"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    # No decompression/redirects: pass bytes through untouched.
    app["upstream"] = aiohttp.ClientSession(
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
        cookie_jar=aiohttp.DummyCookieJar(),
    )


async def on_cleanup(app):
    await app["docker"].close()
    await app["upstream"].close()
    for srv in app["tunnels"].values():
        srv.close()


def make_app():
    app = web.Application(middlewares=[router], client_max_size=0)
    app.router.add_get("/", index)
    app.router.add_get("/go", go)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=LISTEN_PORT)
