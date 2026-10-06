"""The way out of a jailed Chrome's network namespace: a SOCKS5 proxy on a Unix socket, one per browser.

On the server, bwrap gives Chrome its own network namespace with only a loopback interface (`--unshare-net`). Inside
it, `socat` listens on `127.0.0.1:PROXY_PORT` and passes each connection to this proxy's Unix socket, which is mounted
into the jail. Chrome sends every connection there (`--proxy-server=socks5://...`, loopback included), with the host
name unresolved, so the proxy does the DNS lookup itself and connects to the address it checked:

```
Chrome --TCP--> socat (in the jail) --Unix socket--> EgressProxy (in the app) --TCP--> public address
```

Only public addresses are allowed (`ipaddress.is_global`), unless `allow_private`: a page cannot reach the app's own
services, the host, or anything else on a private network, by name or by address, redirects and subresources too.
Only TCP leaves the jail, so QUIC and WebRTC's UDP have nowhere to go.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import struct
from pathlib import Path

PROXY_PORT = 1080
"""The port socat listens on inside the jail, on its own loopback."""

_VERSION = 5
_NO_AUTH = 0
_CONNECT = 1
_IPV4, _DOMAIN, _IPV6 = 1, 3, 4
_SUCCEEDED, _NOT_ALLOWED, _HOST_UNREACHABLE, _REFUSED, _NOT_SUPPORTED = 0, 2, 4, 5, 7
_CONNECT_TIMEOUT = 30
_CLOSE_GRACE = 5


class EgressProxy:
    """A SOCKS5 server (CONNECT only, no authentication) on the Unix socket `path`."""

    def __init__(self, path: Path, *, allow_private: bool = False) -> None:
        self.path = path
        self.allow_private = allow_private
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        self.path.chmod(0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in self._connections:
                task.cancel()
            await asyncio.gather(*self._connections, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        try:
            await self._handle(reader, writer)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, UnicodeError):
            pass  # a name that is not one (bad IDNA, a label over 63 characters) ends the connection like a bad peer
        finally:
            self._connections.discard(task)
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != _VERSION or _NO_AUTH not in methods:
            writer.write(bytes([_VERSION, 0xFF]))
            return
        writer.write(bytes([_VERSION, _NO_AUTH]))
        version, command, _, kind = await reader.readexactly(4)
        if kind == _IPV4:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif kind == _IPV6:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif kind == _DOMAIN:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode('idna')
        else:
            _reply(writer, _NOT_SUPPORTED)
            return
        (port,) = struct.unpack('>H', await reader.readexactly(2))
        if version != _VERSION or command != _CONNECT:
            _reply(writer, _NOT_SUPPORTED)
            return
        status, upstream = await self._connect(host, port)
        _reply(writer, status)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        # When either side is done, the other gets a moment to finish, then both close: socat inside the jail never
        # keeps a half-closed connection, and a silent server must not hold sockets open until the browser closes.
        directions = {asyncio.ensure_future(_pipe(reader, up_writer)), asyncio.ensure_future(_pipe(up_reader, writer))}
        try:
            _, pending = await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                await asyncio.wait(pending, timeout=_CLOSE_GRACE)
        finally:
            for direction in directions:
                direction.cancel()
            await asyncio.gather(*directions, return_exceptions=True)
            up_writer.close()

    async def _connect(
        self, host: str, port: int
    ) -> tuple[int, tuple[asyncio.StreamReader, asyncio.StreamWriter] | None]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return _HOST_UNREACHABLE, None
        addresses = [str(info[4][0]) for info in infos]
        # Any private answer refuses the name, as `browsing.refused_url` does, so DNS cannot pick one for us.
        if not self.allow_private and not all(ipaddress.ip_address(a).is_global for a in addresses):
            return _NOT_ALLOWED, None
        for address in addresses:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(address, port), _CONNECT_TIMEOUT)
            except (OSError, TimeoutError):
                continue
            return _SUCCEEDED, upstream
        return _REFUSED, None


def _reply(writer: asyncio.StreamWriter, status: int) -> None:
    writer.write(bytes([_VERSION, status, 0, _IPV4, 0, 0, 0, 0, 0, 0]))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        with contextlib.suppress(OSError):
            writer.close()
