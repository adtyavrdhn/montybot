"""HTTP to addresses users give us (their MCP servers, and the OAuth servers those name): public addresses only.

A user's MCP server URL, and every URL its OAuth metadata points at, is chosen by someone else. Fetched from our
server, a private address would reach the app's own services, the database or the cloud's metadata endpoint. So the
connection itself is checked: the name is resolved once, every address it has must be public
(`ipaddress.is_global`), and the socket opens to the address that was checked. A name that resolves to a public
address when checked and a private one a moment later (DNS rebinding) cannot slip through, and redirects are checked
the same way as they connect. TLS still verifies the certificate against the name, not the address.

`allow_private` lifts the rule, for tests with servers on 127.0.0.1 (`ALLOW_PRIVATE_NETWORKS`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpcore2
import httpx2
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

TIMEOUT = httpx2.Timeout(30.0, connect=10.0)


class Refused(httpcore2.ConnectError):
    """The address is not one we may open."""


async def public_address(host: str, port: int, *, allow_private: bool) -> str:
    """An address of `host` to connect to. Refused if any of its addresses is not public: DNS must not pick for us."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise Refused(f'{host} could not be found') from error
    addresses = [str(info[4][0]) for info in infos]
    if not addresses:
        raise Refused(f'{host} could not be found')
    if not allow_private and not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise Refused(f'{host} is on a private network')
    return addresses[0]


class PublicOnly(httpcore2.AsyncNetworkBackend):
    """Opens TCP connections to public addresses only, to the very address it checked."""

    def __init__(self, *, allow_private: bool) -> None:
        # httpcore2 defines AnyIOBackend only if anyio is installed, which it is (httpx2 needs it).
        self._inner: httpcore2.AsyncNetworkBackend = httpcore2.AnyIOBackend()  # pyright: ignore[reportAttributeAccessIssue]
        self._allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        address = await public_address(host, port, allow_private=self._allow_private)
        return await self._inner.connect_tcp(
            address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None
    ) -> httpcore2.AsyncNetworkStream:
        raise Refused('unix sockets are not reachable')

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class UntracedTransport(httpx2.AsyncHTTPTransport):
    """Makes no HTTP client spans. A user's MCP server URL can hold a key (in its path or query), and Logfire's httpx
    instrumentation would export every URL it is sent to."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        with suppress_http_instrumentation():
            return await super().handle_async_request(request)


def public_client(*, allow_private: bool, headers: dict[str, str] | None = None) -> httpx2.AsyncClient:
    """A client for user-given URLs. It follows no redirects by itself (OAuth and MCP must see them), takes no proxy
    from the environment (which would connect for us, unchecked), and is not traced."""
    transport = UntracedTransport(trust_env=False)
    # httpx2 has no option for the network backend; its pool takes one, so the pool's own is replaced.
    transport._pool._network_backend = PublicOnly(allow_private=allow_private)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return httpx2.AsyncClient(
        transport=transport, headers=headers, timeout=TIMEOUT, follow_redirects=False, trust_env=False
    )


def check_url(url: str, *, allow_private: bool) -> str:
    """The URL if it may name an MCP or OAuth server: https (or http, only where private networks are allowed, for
    tests), with a host and no user name or password in it. Raises `ValueError` with words for the user."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != 'https' and not (allow_private and parts.scheme == 'http'):
        raise ValueError('The address must start with https://')
    if not parts.hostname:
        raise ValueError('The address has no server name in it')
    if parts.username is not None or parts.password is not None:
        raise ValueError('Put credentials in a header, not in the address')
    return url
