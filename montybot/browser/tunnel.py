"""The Mac tunnel: a run's browser goes out to the web from the user's Mac, so sites see the user's own address.

The Mac app keeps a WebSocket open to the server (`/api/tunnel`, `montybot.tunnel_api`). When a run the user started
launches its browser while that WebSocket is open, the browser's egress proxy is this user's own `EgressProxy`, whose
`dial` step sends each connection down the WebSocket. The Mac resolves the name, refuses any address that is not public
and any port but 80 and 443, connects, and the bytes flow both ways:

```
Chrome -> socat (in the jail) -> EgressProxy (this user's) -> MacTunnel ==WebSocket==> Mac app -> site
```

The server never connects anywhere itself for these browsers. If the Mac goes away, their connections fail
(`HOST_UNREACHABLE`) rather than leave from the server, so a site never sees the address change in the middle of a
session. The next browser the run launches (after the idle reaper, say) asks again.

The wire is binary messages, `kind (1 byte) | stream id (4 bytes, big-endian) | payload`:

| kind     | from   | payload                                                                     |
|----------|--------|-----------------------------------------------------------------------------|
| OPEN 1   | server | the port (2 bytes, big-endian), then the host name, ASCII (IDNA)            |
| OPENED 2 | Mac    | a SOCKS5 status (1 byte): 0 connected, 2 not allowed, 4 unreachable, 5 refused |
| DATA 3   | either | bytes of the stream                                                         |
| CLOSE 4  | either | none: the sender is done with the stream, so the receiver closes it too     |

A connection the browser ends ends at once, as the egress proxy's relay already does, so CLOSE needs no half-close.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import itertools
import socket
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

from montybot.browser.egress import (
    CONNECT_TIMEOUT,
    HOST_UNREACHABLE,
    NOT_ALLOWED,
    REFUSED,
    SUCCEEDED,
    EgressProxy,
    Upstream,
)

OPEN, OPENED, DATA, CLOSE = 1, 2, 3, 4
WEB_PORTS = frozenset({80, 443})
"""The only ports a tunnel carries: the web's. The Mac app refuses any other too."""

_HEADER = struct.Struct('>BI')
_CHUNK = 64 * 1024
_STATUSES = frozenset({SUCCEEDED, NOT_ALLOWED, HOST_UNREACHABLE, REFUSED})
_STALLED = 30
"""Seconds a browser may leave a stream's data unread before the stream is dropped, so it cannot stall the others."""


def message(kind: int, stream: int, payload: bytes = b'') -> bytes:
    return _HEADER.pack(kind, stream) + payload


class _Stream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream: Upstream) -> None:
        self.reader = reader
        """What the browser sends, for the Mac."""
        self.writer = writer
        """What the Mac sends, for the browser."""
        self.upstream = upstream
        """The other end of the pair, which the egress proxy relays to the browser."""
        self.opened: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.pump: asyncio.Task[None] | None = None
        self.handed = False
        """Whether the egress proxy has `upstream`, and closes it itself."""


class MacTunnel:
    """One Mac's WebSocket, carrying many browser connections. `send` sends one binary message on it, and `hang_up`
    closes it, for when the tunnel is closed from this side (another Mac of the same user took over)."""

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        hang_up: Callable[[], Awaitable[None]] | None = None,
        ports: frozenset[int] = WEB_PORTS,
    ) -> None:
        self._send = send
        self._hang_up = hang_up
        self._ports = ports
        self._ids = itertools.count(1)
        self._streams: dict[int, _Stream] = {}
        self._sending = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.closed = False

    async def dial(self, host: str, port: int) -> tuple[int, Upstream | None]:
        """An `EgressProxy` dial step: the Mac connects to `host:port`, and its answer is the status."""
        if self.closed:
            return HOST_UNREACHABLE, None
        if port not in self._ports:
            return NOT_ALLOWED, None
        ours, theirs = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=ours)
        upstream = await asyncio.open_connection(sock=theirs)
        stream_id = next(self._ids)
        stream = self._streams[stream_id] = _Stream(reader, writer, upstream)
        name = host.encode('ascii') if host.isascii() else host.encode('idna')
        try:
            await self._post(message(OPEN, stream_id, struct.pack('>H', port) + name))
            status = await asyncio.wait_for(asyncio.shield(stream.opened), CONNECT_TIMEOUT)
        except (ConnectionError, TimeoutError):
            status = HOST_UNREACHABLE
        except BaseException:
            self._drop(stream_id, tell_mac=True)
            raise
        if status != SUCCEEDED or stream_id not in self._streams:
            self._drop(stream_id, tell_mac=status == HOST_UNREACHABLE)
            return status if status != SUCCEEDED else HOST_UNREACHABLE, None
        stream.pump = asyncio.ensure_future(self._pump(stream_id, stream))
        stream.handed = True
        return SUCCEEDED, upstream

    async def received(self, data: bytes) -> None:
        """One message from the Mac. Anything malformed, or for a stream that is gone, is ignored."""
        if len(data) < _HEADER.size:
            return
        kind, stream_id = _HEADER.unpack_from(data)
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        payload = data[_HEADER.size :]
        if kind == OPENED:
            if not stream.opened.done():
                status = payload[0] if payload else HOST_UNREACHABLE
                stream.opened.set_result(status if status in _STATUSES else HOST_UNREACHABLE)
        elif kind == DATA and stream.opened.done():
            stream.writer.write(payload)
            try:
                await asyncio.wait_for(stream.writer.drain(), _STALLED)
            except (OSError, TimeoutError):
                self._drop(stream_id, tell_mac=True)
        elif kind == CLOSE:
            self._drop(stream_id)

    def close(self) -> None:
        """Every stream ends, and nothing more is dialled. Hangs up the WebSocket if there is a way to."""
        if self.closed:
            return
        self.closed = True
        for stream_id in list(self._streams):
            self._drop(stream_id)
        if self._hang_up is not None:
            self._spawn(self._hang_up())

    async def _pump(self, stream_id: int, stream: _Stream) -> None:
        """What the browser sends, to the Mac, until the browser closes; then CLOSE."""
        try:
            while data := await stream.reader.read(_CHUNK):
                await self._post(message(DATA, stream_id, data))
            if stream_id in self._streams:
                await self._post(message(CLOSE, stream_id))
        except (ConnectionError, OSError):
            pass
        finally:
            stream.pump = None  # this task is ending; `_drop` must not cancel it
            self._drop(stream_id)

    async def _post(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError('the Mac tunnel is closed')
        async with self._sending:
            try:
                await self._send(data)
            except Exception as error:  # whatever the WebSocket raises once it is gone, it means the Mac is gone
                self.close()
                raise ConnectionError('the Mac tunnel is gone') from error

    def _drop(self, stream_id: int, *, tell_mac: bool = False) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        if not stream.opened.done():
            stream.opened.set_result(HOST_UNREACHABLE)
        if stream.pump is not None:
            stream.pump.cancel()
        stream.writer.close()  # the browser's side sees the end, and the egress proxy closes its connection
        if not stream.handed:
            stream.upstream[1].close()
        if tell_mac and not self.closed:
            self._spawn(self._post_quietly(message(CLOSE, stream_id)))

    async def _post_quietly(self, data: bytes) -> None:
        with contextlib.suppress(ConnectionError):
            await self._post(data)

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class Tunnels:
    """The open Mac tunnels by user, and each user's egress proxy, whose sockets live in their own directories under
    `directory` (a jailed browser is given only its user's directory)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._tunnels: dict[str, MacTunnel] = {}
        self._proxies: dict[str, EgressProxy] = {}
        self._starting = asyncio.Lock()

    def connected(self, user_id: str) -> bool:
        tunnel = self._tunnels.get(user_id)
        return tunnel is not None and not tunnel.closed

    def attach(self, user_id: str, tunnel: MacTunnel) -> None:
        """The user's Mac opened its tunnel. The newest Mac wins: an older tunnel of the same user is closed."""
        previous = self._tunnels.get(user_id)
        self._tunnels[user_id] = tunnel
        if previous is not None and previous is not tunnel:
            previous.close()

    def detach(self, user_id: str, tunnel: MacTunnel) -> None:
        tunnel.close()
        if self._tunnels.get(user_id) is tunnel:
            del self._tunnels[user_id]

    async def egress(self, user_id: str) -> Path:
        """The user's egress proxy socket, started on first use. It dials through whichever tunnel the user has open
        at the time, and fails every connection while none is."""
        async with self._starting:
            proxy = self._proxies.get(user_id)
            if proxy is None:
                directory = self.directory / hashlib.sha256(user_id.encode()).hexdigest()[:16]
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                proxy = EgressProxy(directory / 'proxy.sock', dial=functools.partial(self._dial, user_id))
                await proxy.start()
                self._proxies[user_id] = proxy
            return proxy.path

    async def aclose(self) -> None:
        for tunnel in list(self._tunnels.values()):
            tunnel.close()
        self._tunnels.clear()
        await asyncio.gather(*(proxy.stop() for proxy in self._proxies.values()), return_exceptions=True)
        self._proxies.clear()

    async def _dial(self, user_id: str, host: str, port: int) -> tuple[int, Upstream | None]:
        tunnel = self._tunnels.get(user_id)
        if tunnel is None:
            return HOST_UNREACHABLE, None
        return await tunnel.dial(host, port)
