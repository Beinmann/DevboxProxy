"""End-to-end test with a fake Docker API and a fake upstream app.

The fake container is named "127.0.0.1" so the proxy's name-based upstream
lookup lands on localhost. Run: python -m unittest discover tests
"""
import asyncio
import os
import sys
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


def fake_docker():
    async def containers(request):
        net = {"NetworkSettings": {"Networks": {"devbox-net": {}}}}
        boxes = [{"Id": "a", "Names": ["/" + CONTAINER], **net},
                 {"Id": "b", "Names": ["/" + LOOPBACK_ONLY], **net},
                 {"Id": "c", "Names": ["/nonet"], "NetworkSettings": {"Networks": {"bridge": {}}}}]
        if "my-dev-box-v2" in request.query["filters"]:
            return web.json_response(boxes)
        return web.json_response(boxes + [{"Id": "d", "Names": ["/postgres"], **net}])
    a = web.Application()
    a.router.add_get("/containers/json", containers)
    return a


def fake_upstream():
    async def hello(request):
        return web.json_response({"path": request.raw_path, "host": request.headers["Host"],
                                  "xfh": request.headers["X-Forwarded-Host"]})

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
        self.docker = TestServer(fake_docker())
        self.up = TestServer(fake_upstream())
        await self.docker.start_server()
        await self.up.start_server()
        proxy_app.DOCKER_API = f"http://127.0.0.1:{self.docker.port}"
        self.connector = await fake_connector_server(self.up.port)
        proxy_app.CONNECTOR = f"127.0.0.1:{self.connector.sockets[0].getsockname()[1]}"
        self.proxy = TestServer(proxy_app.make_app())
        await self.proxy.start_server()
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

    async def test_proxied_host_refused_without_cookie(self):
        async with self.s.get(self.base + "/a", headers={"Host": self.host}) as r:
            self.assertEqual(r.status, 401)
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            async with self.s.ws_connect(self.base + "/ws", headers={"Host": self.host}):
                pass
        async with self.s.get(self.base + f"{proxy_app.HANDOFF_PATH}?t=1.bad",
                              headers={"Host": self.host}, allow_redirects=False) as r:
            self.assertEqual(r.status, 401)

    async def test_handoff_sets_host_cookie_incl_websocket(self):
        async with self._login_cookie():
            pass
        async with self.s.get(self.base + f"/go?c={CONTAINER}&port={self.up.port}",
                              allow_redirects=False) as r:
            loc = r.headers["Location"]
        host = f"{self.up.port}.{CONTAINER}.127.0.0.1.nip.io"  # UI was opened via an IP
        self.assertTrue(loc.startswith(f"http://{host}:{self.proxy.port}{proxy_app.HANDOFF_PATH}?t="))
        t = loc.split("?t=")[1]
        async with aiohttp.ClientSession() as fresh:  # new browser, no UI cookie
            h = {"Host": f"{host}:{self.proxy.port}"}
            async with fresh.get(f"{self.base}{proxy_app.HANDOFF_PATH}?t={t}", headers=h,
                                 allow_redirects=False) as r:
                self.assertEqual(r.status, 200)
                self.assertIn('url=/', await r.text())
            self.assertIn(proxy_app.COOKIE, r.cookies)
            cookie = {proxy_app.COOKIE: r.cookies[proxy_app.COOKIE].value}
            async with fresh.get(self.base + "/a", headers={**h, "Cookie": f"{proxy_app.COOKIE}={cookie[proxy_app.COOKIE]}; other=1"}) as r:
                self.assertEqual(r.status, 200)
            async with fresh.ws_connect(self.base + "/ws", headers={
                    **h, "Cookie": f"{proxy_app.COOKIE}={cookie[proxy_app.COOKIE]}"}) as ws:
                await ws.send_str("hi")
                self.assertEqual((await ws.receive()).data, "echo:hi")
            # the cookie is bound to its host
            other = {"Host": f"1.{CONTAINER}.localhost", "Cookie": f"{proxy_app.COOKIE}={cookie[proxy_app.COOKIE]}"}
            async with fresh.get(self.base + "/", headers=other) as r:
                self.assertEqual(r.status, 401)

    def test_handoff_expires(self):
        v = proxy_app.handoff_value("h", now=1000)
        self.assertTrue(proxy_app.handoff_valid("h", v, now=1000))
        self.assertFalse(proxy_app.handoff_valid("h", v, now=1000 + proxy_app.HANDOFF_TTL + 1))
        self.assertFalse(proxy_app.handoff_valid("other", v, now=1000))


if __name__ == "__main__":
    unittest.main()
