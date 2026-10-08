"""The Mac tunnel (`tunnel.py`): a user's egress proxy whose connections the user's Mac makes, over the wire.

`FakeMac` is the Mac app's side of the wire. It connects where it is told, loopback included, which the real app
refuses: here the sites are local echo servers.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import tempfile
from collections.abc import Awaitable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from test_egress import connect, echo_server

from montybot.browser.egress import HOST_UNREACHABLE, NOT_ALLOWED, SUCCEEDED
from montybot.browser.tunnel import CLOSE, DATA, OPEN, OPENED, WEB_PORTS, MacTunnel, Tunnels, message

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix='tunnel-', dir='/tmp'))  # a Unix socket path must be short
    yield path
    shutil.rmtree(path, ignore_errors=True)


class FakeMac:
    """Answers OPEN by connecting to `127.0.0.1` (or with `refuse`, a status), as the app would to the host."""

    def __init__(self, *, refuse: int | None = None, silent: bool = False) -> None:
        self.refuse = refuse
        self.silent = silent
        self.tunnel: MacTunnel | None = None
        self.opens: list[tuple[str, int]] = []
        self.closes: list[int] = []
        self._connections: dict[int, asyncio.StreamWriter] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, tunnels: Tunnels, user_id: str, *, ports: frozenset[int] = WEB_PORTS) -> MacTunnel:
        self.tunnel = MacTunnel(self.send, ports=ports)
        tunnels.attach(user_id, self.tunnel)
        return self.tunnel

    async def send(self, data: bytes) -> None:
        kind, stream = struct.unpack_from('>BI', data)
        payload = data[5:]
        if kind == OPEN:
            (port,) = struct.unpack('>H', payload[:2])
            self.opens.append((payload[2:].decode(), port))
            if self.silent:
                return
            if self.refuse is not None:
                self._spawn(self._answer(message(OPENED, stream, bytes([self.refuse]))))
            else:
                self._spawn(self._connect(stream, port))
        elif kind == DATA:
            self._connections[stream].write(payload)
        elif kind == CLOSE:
            self.closes.append(stream)
            if writer := self._connections.pop(stream, None):
                writer.close()

    async def _connect(self, stream: int, port: int) -> None:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        self._connections[stream] = writer
        await self._answer(message(OPENED, stream, bytes([SUCCEEDED])))
        while data := await reader.read(65536):
            await self._answer(message(DATA, stream, data))
        await self._answer(message(CLOSE, stream))

    async def _answer(self, data: bytes) -> None:
        assert self.tunnel is not None
        await self.tunnel.received(data)

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def test_carries_a_connection_through_the_mac(socket_dir: Path) -> None:
    server, port = await echo_server()
    tunnels = Tunnels(socket_dir)
    mac = FakeMac()
    mac.attach(tunnels, 'alice', ports=frozenset({port}))
    try:
        status, reader, writer = await connect(await tunnels.egress('alice'), 'shop.example', port)
        assert status == SUCCEEDED
        writer.write(b'hello')
        assert await asyncio.wait_for(reader.read(100), 5) == b'hello'
        assert await asyncio.wait_for(reader.read(100), 5) == b''  # the site closed, so the Mac did
        writer.close()
        assert mac.opens == [('shop.example', port)]  # the name, unresolved: the Mac resolves it
    finally:
        await tunnels.aclose()
        server.close()


async def test_fails_closed_without_a_mac(socket_dir: Path) -> None:
    """No Mac, no connection: never out through the server instead, which would change the address mid-session."""
    server, port = await echo_server()
    tunnels = Tunnels(socket_dir)
    try:
        proxy = await tunnels.egress('alice')
        status, _, writer = await connect(proxy, '127.0.0.1', port)
        assert status == HOST_UNREACHABLE
        writer.close()
        mac = FakeMac()
        tunnel = mac.attach(tunnels, 'alice', ports=frozenset({port}))
        tunnels.detach('alice', tunnel)  # the Mac went to sleep
        assert not tunnels.connected('alice')
        status, _, writer = await connect(proxy, '127.0.0.1', port)
        assert status == HOST_UNREACHABLE
        writer.close()
        assert mac.opens == []
    finally:
        await tunnels.aclose()
        server.close()


async def test_the_macs_refusal_is_the_browsers_answer(socket_dir: Path) -> None:
    tunnels = Tunnels(socket_dir)
    FakeMac(refuse=NOT_ALLOWED).attach(tunnels, 'alice')
    try:
        status, _, writer = await connect(await tunnels.egress('alice'), 'router.local', 443)
        assert status == NOT_ALLOWED
        writer.close()
    finally:
        await tunnels.aclose()


async def test_only_web_ports_reach_the_mac(socket_dir: Path) -> None:
    tunnels = Tunnels(socket_dir)
    mac = FakeMac()
    mac.attach(tunnels, 'alice')
    try:
        status, _, writer = await connect(await tunnels.egress('alice'), 'shop.example', 22)
        assert status == NOT_ALLOWED
        writer.close()
        assert mac.opens == []
    finally:
        await tunnels.aclose()


async def test_a_mac_that_never_answers_times_out_and_is_told(socket_dir: Path) -> None:
    tunnels = Tunnels(socket_dir)
    mac = FakeMac(silent=True)
    mac.attach(tunnels, 'alice')
    try:
        with patch('montybot.browser.tunnel.CONNECT_TIMEOUT', 0.2):
            status, _, writer = await connect(await tunnels.egress('alice'), 'slow.example', 443)
        assert status == HOST_UNREACHABLE
        writer.close()
        await asyncio.sleep(0.05)
        assert mac.closes == [1]  # so a late connection on the Mac is closed too
    finally:
        await tunnels.aclose()


async def test_the_newest_mac_wins_and_the_old_one_is_hung_up(socket_dir: Path) -> None:
    tunnels = Tunnels(socket_dir)
    hung_up = asyncio.Event()

    async def hang_up() -> None:
        hung_up.set()

    first = MacTunnel(FakeMac().send, hang_up=hang_up)
    tunnels.attach('alice', first)
    second = FakeMac().attach(tunnels, 'alice')
    await asyncio.wait_for(hung_up.wait(), 1)
    assert first.closed and not second.closed and tunnels.connected('alice')
    tunnels.detach('alice', first)  # the old WebSocket's handler finishing must not detach the new Mac
    assert tunnels.connected('alice')
    await tunnels.aclose()


async def test_each_user_has_a_private_socket_directory(socket_dir: Path) -> None:
    """A jailed browser is given only its directory, so it cannot reach another user's Mac."""
    tunnels = Tunnels(socket_dir)
    try:
        alice, bob = await tunnels.egress('alice'), await tunnels.egress('bob')
        assert alice.parent != bob.parent and alice.parent.parent == bob.parent.parent == socket_dir
        assert alice.parent.stat().st_mode & 0o777 == 0o700
        assert await tunnels.egress('alice') == alice  # started once
    finally:
        await tunnels.aclose()
