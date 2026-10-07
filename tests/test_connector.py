"""Connector against a fake Docker unix socket (no real Docker needed)."""
import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "connector"))
import connector  # noqa: E402


def frame(stream, data):
    return struct.pack(">BxxxI", stream, len(data)) + data


class FakeDocker:
    def __init__(self):
        self.exec_body = None
        self.stdin = b""
        self.exec_info = {"Running": True, "ExitCode": None}

    async def handle(self, r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        line = head.split("\r\n")[0]
        clen = int([h.split(":")[1] for h in head.split("\r\n") if h.lower().startswith("content-length")][0])
        body = json.loads(await r.readexactly(clen)) if clen else None

        def reply(status, payload):
            data = json.dumps(payload).encode()
            w.write(f"HTTP/1.1 {status}\\r\\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".replace("\\r\\n", "\r\n").encode() + data)

        if line.startswith("GET /containers/json"):
            assert "my-dev-box-v2" in line or "my-dev-box-v2" in head
            reply("200 OK", [{"Names": ["/box"]}])
        elif "POST /containers/box/exec" in line:
            self.exec_body = body
            reply("201 Created", {"Id": "abc"})
        elif line.startswith("GET /exec/abc/json"):
            reply("200 OK", self.exec_info)
        elif "POST /exec/abc/start" in line:
            assert "Upgrade: tcp" in head
            w.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            w.write(frame(2, b"stderr noise") + frame(1, b"hel") + frame(1, b"lo"))
            await w.drain()
            self.stdin = await r.read()  # until client half-closes
            w.write(frame(1, b"|bye"))
        else:
            reply("404 Not Found", {})
        await w.drain()
        w.close()


class ConnectorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        connector.DOCKER_SOCK = os.path.join(self.dir.name, "d.sock")
        self.fake = FakeDocker()
        self.dsrv = await asyncio.start_unix_server(self.fake.handle, connector.DOCKER_SOCK)
        self.csrv = await asyncio.start_server(connector.handle, "127.0.0.1", 0)
        self.port = self.csrv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.csrv.close()
        self.dsrv.close()
        self.dir.cleanup()

    async def talk(self, first):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(first)
        await w.drain()
        return r, w

    async def test_relays_stdout_only_and_half_close(self):
        r, w = await self.talk(b"box 3000\n")
        self.assertEqual(await r.readline(), b"OK\n")
        w.write(b"GET /")
        w.write_eof()
        self.assertEqual(await asyncio.wait_for(r.read(), 5), b"hello|bye")
        self.assertEqual(self.fake.stdin, b"GET /")
        self.assertEqual(self.fake.exec_body["Cmd"], ["socat", "-", "TCP:127.0.0.1:3000"])

    async def test_failed_exec_start_is_reported(self):
        self.fake.exec_info = {"Running": False, "ExitCode": 127}
        r, w = await self.talk(b"box 3000\n")
        self.assertTrue((await r.readline()).startswith(b"ERR exec exited 127"))

    async def test_rejects_bad_input_and_unknown_container(self):
        for first in (b"box notaport\n", b"../x 80\n", b"box 99999\n", b"just-one\n"):
            r, w = await self.talk(first)
            self.assertTrue((await r.readline()).startswith(b"ERR"), first)
        r, w = await self.talk(b"other 80\n")
        self.assertTrue((await r.readline()).startswith(b"ERR not a running"))
        self.assertIsNone(self.fake.exec_body)


if __name__ == "__main__":
    unittest.main()
