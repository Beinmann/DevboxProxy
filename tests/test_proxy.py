"""End-to-end test with a fake Docker API and a fake upstream app.

The fake container is named "127.0.0.1" so the proxy's name-based upstream
lookup lands on localhost. Run: python -m unittest discover tests
"""
import asyncio
import os
import sys
import time
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import app as proxy_app  # noqa: E402

CONTAINER = "127.0.0.1"
# Direct connects to this name are refused (upstream listens on 127.0.0.1
# only), which triggers the connector fallback.
LOOPBACK_ONLY = "127.0.0.2"


NET = {"NetworkSettings": {"Networks": {"devbox-net": {}}}}


class FakeDocker:
    """Fake Docker API: /containers/json (answers the ancestor filter like
    Docker would) and a /events stream. `boxes` is mutable; emit() pushes an
    event line to every open stream and drop() closes them."""

    def __init__(self):
        self.boxes = [{"Id": "a", "Names": ["/" + CONTAINER], **NET},
                      {"Id": "b", "Names": ["/" + LOOPBACK_ONLY], **NET},
                      {"Id": "c", "Names": ["/nonet"], "NetworkSettings": {"Networks": {"bridge": {}}}}]
        self.others = [{"Id": "d", "Names": ["/postgres"], **NET},
                       {"Id": "e", "Names": ["/docker-socket-proxy"], **NET}]
        self.list_calls = 0
        self.events_params = []
        self.events_status = 200
        self.streams = []
        self.app = web.Application()
        self.app.router.add_get("/containers/json", self.containers)
        self.app.router.add_get("/events", self.events)

    async def containers(self, request):
        self.list_calls += 1
        if "my-dev-box-v2" in request.query["filters"]:
            return web.json_response(self.boxes)
        return web.json_response(self.boxes + self.others)

    async def events(self, request):
        self.events_params.append(dict(request.query))
        if self.events_status != 200:
            return web.Response(status=self.events_status)
        resp = web.StreamResponse()
        await resp.prepare(request)
        q = asyncio.Queue()
        self.streams.append(q)
        try:
            while (line := await q.get()) is not None:
                await resp.write(line)
        finally:
            self.streams.remove(q)
        return resp

    def emit(self, event="start"):
        for q in self.streams:
            q.put_nowait(b'{"Type":"container","Action":"%s"}\n' % event.encode())

    def drop(self):
        for q in list(self.streams):
            q.put_nowait(None)


async def until(cond, timeout=5):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


def fake_upstream():
    async def hello(request):
        return web.json_response({"path": request.raw_path, "host": request.headers["Host"],
                                  "xfh": request.headers["X-Forwarded-Host"],
                                  "cookie": request.headers.get("Cookie", "")})

    async def post(request):
        return web.Response(body=await request.read(), status=201, headers={"X-T": "1"})

    async def redirect(request):
        raise web.HTTPFound("/elsewhere")

    async def ws(request):
        w = web.WebSocketResponse()
        await w.prepare(request)
        async for m in w:
            await w.send_str("echo:" + m.data)
        return w
    a = web.Application()
    a.router.add_get("/ws", ws)
    a.router.add_get("/redir", redirect)
    a.router.add_post("/post", post)
    a.router.add_get("/{tail:.*}", hello)
    return a


async def fake_connector_server(upstream_port):
    """Speaks the connector protocol, relaying to 127.0.0.1:upstream_port."""
    async def on_conn(r, w):
        name, port = (await r.readline()).split()
        if name.decode() != LOOPBACK_ONLY or int(port) != upstream_port:
            w.write(b"ERR nope\n")
            w.close()
            return
        w.write(b"OK\n")
        ur, uw = await asyncio.open_connection("127.0.0.1", upstream_port)
        await asyncio.gather(proxy_app.relay(r, uw), proxy_app.relay(ur, w))
        w.close()
    return await asyncio.start_server(on_conn, "127.0.0.1", 0)


class ProxyBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeDocker()
        self.docker = TestServer(self.fake.app)
        self.up = TestServer(fake_upstream())
        await self.docker.start_server()
        await self.up.start_server()
        proxy_app.DOCKER_API = f"http://127.0.0.1:{self.docker.port}"
        self.connector = await fake_connector_server(self.up.port)
        proxy_app.CONNECTOR = f"127.0.0.1:{self.connector.sockets[0].getsockname()[1]}"
        proxy_app.EVENT_DEBOUNCE, proxy_app.EVENT_RETRY = 0.01, 0.05
        self.proxy = TestServer(proxy_app.make_app())
        await self.proxy.start_server()
        self.watcher = self.proxy.app["devboxes"]
        await until(lambda: self.watcher.streaming and self.watcher.synced)
        self.s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.base = f"http://127.0.0.1:{self.proxy.port}"
        self.host = f"{self.up.port}.{CONTAINER}.localhost:{self.proxy.port}"

    async def asyncTearDown(self):
        await self.s.close()
        self.connector.close()
        for srv in (self.proxy, self.up, self.docker):
            await srv.close()


class ProxyTest(ProxyBase):
    async def test_ui_lists_and_redirects(self):
        async with self.s.get(self.base + "/") as r:
            self.assertIn(CONTAINER, await r.text())
        async with self.s.get(self.base + f"/go?c={CONTAINER}&port=3000", allow_redirects=False,
                              headers={"Host": f"localhost:{self.proxy.port}"}) as r:
            self.assertEqual(r.status, 302)
            self.assertEqual(r.headers["Location"], f"http://3000.{CONTAINER}.localhost:{self.proxy.port}/")
        async with self.s.get(self.base + "/go?c=evil&port=3000", allow_redirects=False) as r:
            self.assertEqual(r.status, 400)

    async def test_ui_flags_unusable_containers(self):
        async with self.s.get(self.base + "/") as r:
            html = await r.text()
        self.assertIn("postgres", html)
        self.assertIn("non-devbox image", html)
        self.assertIn("not on devbox-net", html)
        self.assertLess(html.index("<b>" + LOOPBACK_ONLY), html.index("<b>nonet"))  # usable first
        self.assertEqual(html.count("action=/go"), 2)  # flagged rows have no Open form
        async with self.s.get(self.base + "/?hide_nondevbox=1") as r:
            self.assertNotIn("postgres</b>", await r.text())
        async with self.s.get(self.base + "/?hide_nonet=1") as r:
            self.assertNotIn("nonet</b>", await r.text())
        for c in ("postgres", "nonet"):
            async with self.s.get(self.base + f"/go?c={c}&port=3000", allow_redirects=False) as r:
                self.assertEqual(r.status, 400)

    async def test_http_forwarding(self):
        async with self.s.get(self.base + "/a/b?x=1", headers={"Host": self.host}) as r:
            j = await r.json()
        self.assertEqual(j["path"], "/a/b?x=1")
        self.assertEqual(j["host"], self.host)

    async def test_rewrite_host_sends_localhost_upstream(self):
        proxy_app.REWRITE_HOST = True
        self.addCleanup(setattr, proxy_app, "REWRITE_HOST", False)
        async with self.s.get(self.base + "/", headers={"Host": self.host}) as r:
            j = await r.json()
        self.assertEqual(j["host"], f"localhost:{self.up.port}")
        self.assertEqual(j["xfh"], self.host)

    async def test_post_and_redirect_passthrough(self):
        async with self.s.post(self.base + "/post", data=b"payload", headers={"Host": self.host}) as r:
            self.assertEqual((r.status, await r.read(), r.headers["X-T"]), (201, b"payload", "1"))
        async with self.s.get(self.base + "/redir", headers={"Host": self.host}, allow_redirects=False) as r:
            self.assertEqual((r.status, r.headers["Location"]), (302, "/elsewhere"))

    async def test_websocket(self):
        async with self.s.ws_connect(self.base + "/ws", headers={"Host": self.host}) as ws:
            await ws.send_str("hi")
            self.assertEqual((await ws.receive()).data, "echo:hi")

    async def test_loopback_only_via_connector(self):
        host = f"{self.up.port}.{LOOPBACK_ONLY}.localhost:{self.proxy.port}"
        async with self.s.post(self.base + "/post", data=b"x" * 200000, headers={"Host": host}) as r:
            self.assertEqual((r.status, len(await r.read())), (201, 200000))
        async with self.s.get(self.base + "/a", headers={"Host": host}) as r:
            self.assertEqual((await r.json())["path"], "/a")
        async with self.s.ws_connect(self.base + "/ws", headers={"Host": host}) as ws:
            await ws.send_str("hi")
            self.assertEqual((await ws.receive()).data, "echo:hi")

    async def test_garbage_from_upstream_is_502(self):
        async def junk(r, w):
            w.write(b"OCI runtime exec failed: nope\n")
            w.close()
        srv = await asyncio.start_server(junk, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        async with self.s.get(self.base + "/", headers={"Host": f"{port}.{CONTAINER}.localhost"}) as r:
            self.assertEqual(r.status, 502)
        srv.close()

    async def test_stalled_upstream_is_502_not_a_hang(self):
        proxy_app.UPSTREAM_TIMEOUT = 0.5
        held = []
        srv = await asyncio.start_server(lambda r, w: held.append(w), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        async with self.s.get(self.base + "/", headers={"Host": f"{port}.{CONTAINER}.localhost"},
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
            self.assertEqual(r.status, 502)
            self.assertIn("Cannot reach", await r.text())
        srv.close()

    async def test_refused_without_connector_is_502(self):
        proxy_app.CONNECTOR = ""
        host = f"{self.up.port}.{LOOPBACK_ONLY}.localhost"
        async with self.s.get(self.base + "/", headers={"Host": host}) as r:
            self.assertEqual(r.status, 502)

    async def test_unknown_container_and_dead_port(self):
        async with self.s.get(self.base + "/", headers={"Host": f"80.nope.localhost:{self.proxy.port}"}) as r:
            self.assertEqual(r.status, 404)
        async with self.s.get(self.base + "/", headers={"Host": f"1.{CONTAINER}.localhost"}) as r:
            self.assertEqual(r.status, 502)


class AuthTest(ProxyBase):
    """Same stack with a token set; only the auth-specific tests run."""
    TOKEN = "s3cret-token"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        proxy_app.TOKEN = self.TOKEN

    async def asyncTearDown(self):
        proxy_app.TOKEN = ""
        await super().asyncTearDown()

    def _login_cookie(self):
        return self.s.post(self.base + "/login", data={"token": self.TOKEN}, allow_redirects=False)

    async def test_ui_requires_login(self):
        async with self.s.get(self.base + "/") as r:
            self.assertEqual(r.status, 401)
            self.assertNotIn(CONTAINER, await r.text())
        async with self.s.get(self.base + f"/go?c={CONTAINER}&port=3000", allow_redirects=False) as r:
            self.assertEqual(r.status, 401)
        async with self.s.post(self.base + "/login", data={"token": "wrong"}) as r:
            self.assertEqual(r.status, 401)
        async with self._login_cookie() as r:
            self.assertEqual(r.status, 303)
        async with self.s.get(self.base + "/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn(CONTAINER, await r.text())

    async def test_icon_served_without_login_and_linked(self):
        for path in ("/favicon.svg", "/favicon.ico"):
            async with self.s.get(self.base + path) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.content_type, "image/svg+xml")
                self.assertIn("<svg", await r.text())
        async with self.s.get(self.base + "/") as r:  # login page
            self.assertEqual(r.status, 401)
            self.assertIn("rel=icon", await r.text())
        async with self._login_cookie():
            pass
        async with self.s.get(self.base + "/") as r:  # main page
            self.assertIn("rel=icon", await r.text())

    async def _open(self, c=CONTAINER, port=None, ui_host="127.0.0.1"):
        """What the UI's Open button does; returns the capability host (with port)."""
        async with self._login_cookie():
            pass
        async with self.s.get(self.base + f"/go?c={c}&port={port or self.up.port}",
                              allow_redirects=False, headers={"Host": f"{ui_host}:{self.proxy.port}"}) as r:
            self.assertEqual(r.status, 302)
            return r.headers["Location"].split("/")[2]

    async def test_go_mints_capability_host(self):
        host = await self._open()  # UI opened via an IP -> nip.io
        label = proxy_app.cap_label(self.up.port, CONTAINER)
        self.assertEqual(host, f"{self.up.port}.{CONTAINER}.{label}.127-0-0-1.nip.io:{self.proxy.port}")
        self.assertLessEqual(len(label), 63)  # one DNS label
        self.assertEqual(await self._open(), host)  # stable, so the origin (localStorage) survives
        ui = {"Host": f"localhost:{self.proxy.port}"}  # UI opened as localhost: *.localhost links
        async with self.s.post(self.base + "/login", data={"token": self.TOKEN}, headers=ui):
            pass
        async with self.s.get(self.base + f"/go?c={CONTAINER}&port=3000", allow_redirects=False,
                              headers=ui) as r:
            self.assertRegex(r.headers["Location"], r"^http://3000\.127\.0\.0\.1\.[0-9a-f]{42}\.localhost:\d+/$")

    async def test_capability_host_needs_no_cookie_incl_subresources_and_websocket(self):
        host = await self._open()
        async with aiohttp.ClientSession() as fresh:  # no cookies at all, like a sandboxed iframe
            h = {"Host": host, "Origin": "null", "Sec-Fetch-Site": "cross-site"}
            for path in ("/", "/sdk.js", "/widgets/a.js"):
                async with fresh.get(self.base + path, headers=h) as r:
                    self.assertEqual(r.status, 200, path)
                    self.assertEqual((await r.json())["path"], path)
                    self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
            async with fresh.post(self.base + "/post", data=b"x", headers=h) as r:
                self.assertEqual(r.status, 201)
            async with fresh.ws_connect(self.base + "/ws", headers=h) as ws:
                await ws.send_str("hi")
                self.assertEqual((await ws.receive()).data, "echo:hi")

    async def test_capability_host_via_localhost_and_loopback_connector(self):
        label = proxy_app.cap_label(self.up.port, LOOPBACK_ONLY)
        host = f"{self.up.port}.{LOOPBACK_ONLY}.{label}.localhost:{self.proxy.port}"
        async with self.s.get(self.base + "/a", headers={"Host": host}) as r:
            self.assertEqual((await r.json())["path"], "/a")

    async def test_proxied_host_refused_without_valid_capability(self):
        good = proxy_app.cap_label(self.up.port, CONTAINER)
        other_port = proxy_app.cap_label(self.up.port + 1, CONTAINER)
        other_box = proxy_app.cap_label(self.up.port, LOOPBACK_ONLY)
        expired = proxy_app.cap_label(self.up.port, CONTAINER, now=time.time() - 3 * proxy_app.CAP_TTL)
        tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
        hosts = [f"{self.up.port}.{CONTAINER}.localhost",           # no label
                 f"{self.up.port}.{CONTAINER}.{other_port}.localhost",
                 f"{self.up.port}.{CONTAINER}.{other_box}.localhost",
                 f"{self.up.port}.{CONTAINER}.{expired}.localhost",
                 f"{self.up.port}.{CONTAINER}.{tampered}.localhost",
                 f"{self.up.port}.{CONTAINER}.{good.upper()}x.localhost",
                 f"{self.up.port}.{CONTAINER}.nothex.localhost"]
        async with self._login_cookie():  # even a logged-in UI cookie doesn't help
            pass
        for host in hosts:
            async with self.s.get(self.base + "/a", headers={"Host": host}) as r:
                self.assertEqual(r.status, 401, host)
            with self.assertRaises(aiohttp.WSServerHandshakeError):
                async with self.s.ws_connect(self.base + "/ws", headers={"Host": host}):
                    pass
        # label for another container doesn't work with a rewritten container part
        async with self.s.get(self.base + "/a", headers={
                "Host": f"{self.up.port}.{LOOPBACK_ONLY}.{good}.localhost"}) as r:
            self.assertEqual(r.status, 401)
        # a token-less test above would have worked; a different token invalidates the label
        proxy_app.TOKEN = "rotated"
        async with self.s.get(self.base + "/a", headers={
                "Host": f"{self.up.port}.{CONTAINER}.{good}.localhost"}) as r:
            self.assertEqual(r.status, 401)

    def test_capability_expiry_and_window(self):
        ttl = proxy_app.CAP_TTL
        label = proxy_app.cap_label(80, "box", now=1000)
        self.assertEqual(label, proxy_app.cap_label(80, "box", now=ttl - 1))  # same window
        self.assertNotEqual(label, proxy_app.cap_label(80, "box", now=1000 + ttl))
        self.assertTrue(proxy_app.cap_valid(80, "box", label, now=1000 + ttl))  # lives >= one TTL
        self.assertFalse(proxy_app.cap_valid(80, "box", label, now=3 * ttl))
        self.assertFalse(proxy_app.cap_valid(80, "box", label, now=1000 + 2 * ttl + 1))
        self.assertFalse(proxy_app.cap_valid(81, "box", label, now=1000))
        self.assertFalse(proxy_app.cap_valid(80, "other", label, now=1000))

    async def test_app_cookies_pass_through_and_log_hides_capability(self):
        host = await self._open()
        with self.assertLogs("devbox-proxy", "INFO") as logs:
            async with aiohttp.ClientSession() as fresh, fresh.get(self.base + "/a", headers={
                    "Host": host, "Cookie": f"{proxy_app.COOKIE}=x; theme=dark"}) as r:
                self.assertEqual((await r.json())["cookie"], f"{proxy_app.COOKIE}=x; theme=dark")
        self.assertNotIn(proxy_app.cap_label(self.up.port, CONTAINER), "\n".join(logs.output))

    async def test_ui_login_unchanged_and_referrer_policy(self):
        async with self.s.get(self.base + "/") as r:
            self.assertEqual((r.status, r.headers["Referrer-Policy"]), (401, "no-referrer"))
        async with self._login_cookie() as r:
            self.assertEqual(r.status, 303)
            self.assertIn(proxy_app.COOKIE, r.cookies)
        async with self.s.get(self.base + "/") as r:
            self.assertEqual((r.status, r.headers["Referrer-Policy"]), (200, "no-referrer"))

    async def test_old_handoff_path_is_just_forwarded_or_refused(self):
        async with self.s.get(self.base + "/__devbox_auth?t=1.bad",
                              headers={"Host": f"{self.up.port}.{CONTAINER}.localhost"}) as r:
            self.assertEqual(r.status, 401)


class AllowlistTest(ProxyBase):
    """The allowed-container set comes from Docker's event stream, not per request."""

    async def get(self, container, path="/a"):
        host = f"{self.up.port}.{container}.localhost"
        async with self.s.get(self.base + path, headers={"Host": host}) as r:
            return r.status

    async def test_no_docker_calls_per_request(self):
        before = self.fake.list_calls
        for _ in range(10):
            self.assertEqual(await self.get(CONTAINER), 200)
        async with self.s.ws_connect(self.base + "/ws", headers={"Host": self.host}):
            pass
        self.assertEqual(self.fake.list_calls, before)
        # asked for events since shortly before the fill, limited to relevant ones
        params = self.fake.events_params[0]
        self.assertIn("since", params)
        self.assertIn('"connect"', params["filters"])

    async def test_non_devbox_containers_stay_blocked(self):
        for name in ("postgres", "docker-socket-proxy", "nonet", "nope"):
            self.assertEqual(await self.get(name), 404, name)
        self.assertEqual(self.watcher.names, {CONTAINER, LOOPBACK_ONLY})

    async def test_events_update_the_set(self):
        new = {"Id": "n", "Names": ["/127.0.0.3"], **NET}
        self.assertEqual(await self.get("127.0.0.3"), 404)
        self.fake.boxes.append(new)
        self.fake.emit("start")
        await until(lambda: "127.0.0.3" in self.watcher.names)
        self.fake.boxes.remove(new)
        self.fake.emit("die")
        await until(lambda: "127.0.0.3" not in self.watcher.names)
        self.assertEqual(await self.get("127.0.0.3"), 404)
        # a non-devbox container appearing is listed by Docker but never allowed
        self.fake.others.append({"Id": "x", "Names": ["/127.0.0.4"], **NET})
        calls = self.fake.list_calls
        self.fake.emit("start")
        await until(lambda: self.fake.list_calls > calls)
        await asyncio.sleep(0.1)
        self.assertEqual(await self.get("127.0.0.4"), 404)

    async def test_event_bursts_are_coalesced(self):
        calls = self.fake.list_calls
        for _ in range(20):
            self.fake.emit("exec")
        await asyncio.sleep(0.3)
        self.assertLessEqual(self.fake.list_calls - calls, 4)  # 2 calls per re-list

    async def test_resync_after_reconnect(self):
        new = {"Id": "n", "Names": ["/127.0.0.3"], **NET}
        self.fake.events_status = 503  # stream can't come back yet
        self.fake.drop()
        await until(lambda: not self.watcher.streaming)
        self.fake.boxes.append(new)  # changed while we were blind
        # while the stream is down the proxy asks Docker directly (still correct)
        before = self.fake.list_calls
        self.assertEqual(await self.get("127.0.0.3"), 502)  # allowed (nothing listens there), not 404
        self.assertGreater(self.fake.list_calls, before)
        self.fake.events_status = 200
        await until(lambda: self.watcher.streaming and self.watcher.synced)
        self.assertIn("127.0.0.3", self.watcher.names)
        before = self.fake.list_calls
        self.assertEqual(await self.get("127.0.0.3"), 502)
        self.assertEqual(self.fake.list_calls, before)  # cached again

    async def test_quiet_stream_is_resynced_periodically(self):
        proxy_app.RESYNC_INTERVAL = 0.1
        self.addCleanup(setattr, proxy_app, "RESYNC_INTERVAL", 60.0)
        self.fake.drop()  # reconnect picks up the short interval
        await until(lambda: self.watcher.streaming and self.watcher.synced)
        self.fake.boxes.append({"Id": "n", "Names": ["/127.0.0.3"], **NET})  # no event emitted
        await until(lambda: "127.0.0.3" in self.watcher.names)

    async def test_starts_even_if_docker_is_down_at_first(self):
        await self.proxy.close()
        self.fake.events_status = 503
        proxy_app.EVENT_RETRY = 0.05
        self.proxy = TestServer(proxy_app.make_app())
        await self.proxy.start_server()
        self.base = f"http://127.0.0.1:{self.proxy.port}"
        self.watcher = self.proxy.app["devboxes"]
        self.assertFalse(self.watcher.streaming)
        self.assertEqual(await self.get(CONTAINER), 200)  # direct lookup fallback
        self.fake.events_status = 200
        await until(lambda: self.watcher.streaming and self.watcher.synced)


if __name__ == "__main__":
    unittest.main()
