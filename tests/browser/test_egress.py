"""`EgressProxy`, the jailed Chrome's only way out: SOCKS5 CONNECT to public addresses only."""

from __future__ import annotations

import asyncio
import shutil
import socket
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path

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
