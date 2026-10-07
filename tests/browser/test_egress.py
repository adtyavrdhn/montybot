"""`EgressProxy`, the jailed Chrome's only way out: SOCKS5 CONNECT to public addresses only."""

from __future__ import annotations

import asyncio
import shutil
import socket
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from montybot.browser.egress import EgressProxy

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix='egress-', dir='/tmp'))  # a Unix socket path must be short
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def echo_server() -> tuple[asyncio.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    return server, server.sockets[0].getsockname()[1]


async def connect(proxy: Path, host: str, port: int) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    """A SOCKS5 CONNECT through the proxy, as Chrome makes it; returns the reply's status."""
    reader, writer = await asyncio.open_unix_connection(str(proxy))
    writer.write(b'\x05\x01\x00')
    assert await reader.readexactly(2) == b'\x05\x00'
    try:
        address = b'\x01' + socket.inet_aton(host)
    except OSError:
        address = b'\x03' + bytes([len(host)]) + host.encode()
    writer.write(b'\x05\x01\x00' + address + struct.pack('>H', port))
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


async def test_proxy_can_restart_on_its_socket_path(socket_dir: Path) -> None:
    path = socket_dir / 'egress.sock'
    path.touch()  # a crashed sidecar can leave a stale socket in its volume
    proxy = EgressProxy(path)
    await proxy.start()
    await proxy.stop()
    assert not path.exists()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'\x05\x01\x00')
        assert await reader.readexactly(2) == b'\x05\x00'
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()


async def test_refuses_private_addresses_and_names(socket_dir: Path) -> None:
    server, port = await echo_server()
    proxy = EgressProxy(socket_dir / 'egress.sock')
    await proxy.start()
    try:
        for host in ('127.0.0.1', 'localhost', '10.0.0.1', '169.254.169.254'):
            status, _, writer = await connect(proxy.path, host, port)
            writer.close()
            assert status == 2, host  # connection not allowed by ruleset
    finally:
        await proxy.stop()
        server.close()


async def test_stalled_handshake_closes_and_frees_its_slot(socket_dir: Path) -> None:
    proxy = EgressProxy(socket_dir / 'egress.sock')
    await proxy.start()
    try:
        with patch('montybot.browser.egress._HANDSHAKE_TIMEOUT', 0.01):
            reader, writer = await asyncio.open_unix_connection(str(proxy.path))
            assert await asyncio.wait_for(reader.read(), 1) == b''
            writer.close()
            await writer.wait_closed()
        assert not proxy._connections
    finally:
        await proxy.stop()


async def test_too_many_connections_do_not_block_the_proxy(socket_dir: Path) -> None:
    proxy = EgressProxy(socket_dir / 'egress.sock')
    await proxy.start()
    try:
        with patch('montybot.browser.egress._MAX_CONNECTIONS', 1):
            first, writer = await asyncio.open_unix_connection(str(proxy.path))
            # The greeting confirms the first connection occupies the single slot.
            writer.write(b'\x05\x01\x00')
            assert await first.readexactly(2) == b'\x05\x00'
            other, other_writer = await asyncio.open_unix_connection(str(proxy.path))
            assert await asyncio.wait_for(other.read(), 1) == b''
            other_writer.close()
            writer.close()
            await other_writer.wait_closed()
            await writer.wait_closed()
    finally:
        await proxy.stop()


async def test_established_connection_outlives_handshake_deadline(socket_dir: Path) -> None:
    server, port = await echo_server()
    proxy = EgressProxy(socket_dir / 'egress.sock', allow_private=True)
    await proxy.start()
    try:
        with patch('montybot.browser.egress._HANDSHAKE_TIMEOUT', 0.05):
            status, reader, writer = await connect(proxy.path, '127.0.0.1', port)
            assert status == 0
            await asyncio.sleep(0.1)
            writer.write(b'hello')
            assert await asyncio.wait_for(reader.readexactly(5), 1) == b'hello'
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.stop()
        server.close()


async def test_carries_a_connection_where_allowed(socket_dir: Path) -> None:
    server, port = await echo_server()
    proxy = EgressProxy(socket_dir / 'egress.sock', allow_private=True)
    await proxy.start()
    assert proxy.path.stat().st_mode & 0o777 == 0o600
    try:
        status, reader, writer = await connect(proxy.path, 'localhost', port)
        assert status == 0
        writer.write(b'hello')
        assert await reader.read(100) == b'hello'
        writer.close()
    finally:
        await proxy.stop()
        server.close()
