"""Loopback connector for devbox-proxy (ticket 006).

Lets the proxy reach servers bound to 127.0.0.1 inside a devbox. A client
connects, sends one line "<container> <port>\\n", gets "OK\\n" (or "ERR
<reason>\\n"), and from then on the TCP stream is relayed to
127.0.0.1:<port> *inside that container's network namespace* via
`docker exec <container> socat - TCP:127.0.0.1:<port>`.

This is the only component holding the raw Docker socket, so its surface is
deliberately tiny: it can only run that one fixed command, only in running
containers of the devbox image, and is only reachable on the proxy's
internal network. stdlib only.
"""
import asyncio
import json
import os
import re
import struct
from urllib.parse import quote

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
DEVBOX_IMAGE = os.environ.get("DEVBOX_IMAGE", "my-dev-box-v2")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


async def read_head(reader):
    """Read an HTTP response head; returns (status, headers dict)."""
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return status, headers


async def docker_open(method, path, body=None, upgrade=False):
    reader, writer = await asyncio.open_unix_connection(DOCKER_SOCK)
    data = json.dumps(body).encode() if body is not None else b""
    head = [f"{method} {path} HTTP/1.1", "Host: docker", "Content-Type: application/json",
            f"Content-Length: {len(data)}"]
    head.append("Connection: Upgrade\r\nUpgrade: tcp" if upgrade else "Connection: close")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await writer.drain()
    return reader, writer


async def docker_json(method, path, body=None):
    reader, writer = await docker_open(method, path, body)
    try:
        status, headers = await read_head(reader)
        payload = await reader.read()  # Connection: close
        if headers.get("transfer-encoding") == "chunked":
            payload = dechunk(payload)
        return status, (json.loads(payload) if payload.strip() else None)
    finally:
        writer.close()


def dechunk(data):
    out = b""
    while data:
        size, _, rest = data.partition(b"\r\n")
        n = int(size.split(b";")[0] or b"0", 16)
        if n == 0:
            break
        out += rest[:n]
        data = rest[n + 2:]
    return out


async def is_devbox(name):
    flt = json.dumps({"ancestor": [DEVBOX_IMAGE], "status": ["running"]})
    status, data = await docker_json("GET", f"/containers/json?filters={quote(flt)}")
    if status != 200:
        return False
    return any(("/" + name) in c.get("Names", []) for c in data)


async def pump_from_docker(reader, writer):
    """Demultiplex Docker's exec stream; forward stdout frames only."""
    try:
        while True:
            header = await reader.readexactly(8)
            stream, size = header[0], struct.unpack(">I", header[4:])[0]
            payload = await reader.readexactly(size) if size else b""
            if stream == 1:
                writer.write(payload)
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass


async def pump_to_docker(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()  # half-close: lets socat flush and exit
    except ConnectionError:
        pass


async def handle(creader, cwriter):
    dwriter = None
    try:
        line = (await asyncio.wait_for(creader.readline(), 5)).decode("latin-1").split()
        if len(line) != 2 or not NAME_RE.match(line[0]) or not line[1].isdigit() \
                or not 1 <= int(line[1]) <= 65535:
            cwriter.write(b"ERR bad request\n")
            return
        name, port = line[0], int(line[1])
        if not await is_devbox(name):
            cwriter.write(b"ERR not a running devbox container\n")
            return
        status, exe = await docker_json("POST", f"/containers/{name}/exec", {
            "Cmd": ["socat", "-", f"TCP:127.0.0.1:{port}"],
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True, "Tty": False,
        })
        if status != 201:
            cwriter.write(b"ERR exec create failed\n")
            return
        dreader, dwriter = await docker_open(
            "POST", f"/exec/{exe['Id']}/start", {"Detach": False, "Tty": False}, upgrade=True)
        status, _ = await read_head(dreader)
        if status not in (101, 200):
            cwriter.write(b"ERR exec start failed\n")
            return
        cwriter.write(b"OK\n")
        await cwriter.drain()
        tasks = [asyncio.ensure_future(pump_from_docker(dreader, cwriter)),
                 asyncio.ensure_future(pump_to_docker(creader, dwriter))]
        # Done when docker's side ends (socat exited / upstream closed).
        await tasks[0]
        tasks[1].cancel()
    except Exception as e:  # never take the connector down over one connection
        try:
            cwriter.write(f"ERR {type(e).__name__}\n".encode())
        except Exception:
            pass
    finally:
        for w in (dwriter, cwriter):
            if w is not None:
                w.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
