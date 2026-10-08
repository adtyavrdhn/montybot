"""`/api/tunnel`, the Mac app's door to the tunnel: who may open it, and the wire through it end to end."""

from __future__ import annotations

import shutil
import socket
import struct
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from sammy.browser.cdp import ChromiumCDPBackend
from sammy.browser.egress import SUCCEEDED
from sammy.browser.tunnel import DATA, OPEN, OPENED, Place, Tunnels, message
from sammy.resources import routed_backend_factory
from sammy.tunnel_api import REPLACED, mac_tunnel


@dataclass
class FakeResources:
    tunnels: Tunnels | None


class SignedInAs:
    """The session `SessionMiddleware` would give: a `user` query parameter signs the WebSocket in."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        user = dict(part.split('=') for part in scope['query_string'].decode().split('&') if part).get('user')
        scope['session'] = {'user_id': user} if user else {}
        await self.app(scope, receive, send)


@pytest.fixture
def tunnels() -> Iterator[Tunnels]:
    path = Path(tempfile.mkdtemp(prefix='tunnel-', dir='/tmp'))  # a Unix socket path must be short
    yield Tunnels(path)
    shutil.rmtree(path, ignore_errors=True)


def client(tunnels: Tunnels | None) -> TestClient:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[dict[str, FakeResources]]:
        yield {'resources': FakeResources(tunnels)}
        if tunnels is not None:
            await tunnels.aclose()

    app = Starlette(routes=[WebSocketRoute('/api/tunnel', mac_tunnel)], lifespan=lifespan)
    return TestClient(SignedInAs(app))  # type: ignore[arg-type]  # Starlette's own app type is narrower than ASGIApp


def refused(test: TestClient, url: str, headers: dict[str, str] | None = None) -> int:
    with pytest.raises(WebSocketDisconnect) as error, test.websocket_connect(url, headers=headers or {}) as ws:
        ws.receive_bytes()
    return error.value.code


def test_refuses_the_signed_out_web_pages_and_a_server_without_tunnels(tunnels: Tunnels) -> None:
    with client(tunnels) as test:
        assert refused(test, '/api/tunnel') == 1008
        assert refused(test, '/api/tunnel?user=alice', {'Origin': 'https://evil.example'}) == 1008
    with client(None) as test:
        assert refused(test, '/api/tunnel?user=alice') == 1008


def test_a_second_mac_replaces_the_first(tunnels: Tunnels) -> None:
    with (
        client(tunnels) as test,
        test.websocket_connect('/api/tunnel?user=alice') as first,
        test.websocket_connect('/api/tunnel?user=alice'),
    ):
        with pytest.raises(WebSocketDisconnect) as error:
            first.receive_bytes()
        assert error.value.code == REPLACED
        assert tunnels.connected('alice')
    assert not tunnels.connected('alice')  # the Mac's WebSocket closed, and the tunnel with it


def test_a_browser_through_the_mac_takes_its_clock_and_language(tunnels: Tunnels) -> None:
    """The Mac says where it is; a browser started on its proxy gets that zone and language, junk left out."""
    routed = routed_backend_factory('sammy.engines:chromium_cdp_server', tunnels.place_of)
    assert routed is not None
    place = {'X-Sammy-Timezone': 'America/Toronto', 'X-Sammy-Locale': 'en-CA'}
    with client(tunnels) as test, test.websocket_connect('/api/tunnel?user=alice', headers=place):
        proxy = test.portal.call(tunnels.egress, 'alice')  # type: ignore[union-attr]  # set inside `with`
        backend = routed(proxy)
        assert isinstance(backend, ChromiumCDPBackend)
        assert (backend.options.timezone, backend.options.locale) == ('America/Toronto', 'en-CA')
        assert tunnels.place_of(Path('/elsewhere.sock')) == Place()
    junk = {'X-Sammy-Timezone': 'Mars/Base', 'X-Sammy-Locale': 'en CA; rm -rf'}
    with client(tunnels) as test, test.websocket_connect('/api/tunnel?user=bob', headers=junk):
        proxy = test.portal.call(tunnels.egress, 'bob')  # type: ignore[union-attr]
        assert tunnels.place_of(proxy) == Place()


def test_a_browser_connection_crosses_the_websocket(tunnels: Tunnels) -> None:
    """A SOCKS5 CONNECT on alice's proxy becomes OPEN on her Mac's WebSocket; what each side sends reaches the other."""
    with client(tunnels) as test, test.websocket_connect('/api/tunnel?user=alice') as mac:
        proxy = test.portal.call(tunnels.egress, 'alice')  # type: ignore[union-attr]  # set inside `with`
        browser = socket.socket(socket.AF_UNIX)
        browser.settimeout(5)
        browser.connect(str(proxy))
        browser.sendall(b'\x05\x01\x00')
        assert browser.recv(2) == b'\x05\x00'
        browser.sendall(b'\x05\x01\x00\x03' + bytes([12]) + b'shop.example' + struct.pack('>H', 443))

        opened = mac.receive_bytes()
        kind, stream = struct.unpack_from('>BI', opened)
        assert (kind, opened[5:]) == (OPEN, struct.pack('>H', 443) + b'shop.example')
        mac.send_bytes(message(OPENED, stream, bytes([SUCCEEDED])))
        assert browser.recv(10)[1] == SUCCEEDED

        browser.sendall(b'GET / HTTP/1.1\r\n\r\n')
        assert mac.receive_bytes() == message(DATA, stream, b'GET / HTTP/1.1\r\n\r\n')
        mac.send_bytes(message(DATA, stream, b'HTTP/1.1 200 OK\r\n\r\n'))
        assert browser.recv(100) == b'HTTP/1.1 200 OK\r\n\r\n'
        browser.close()
