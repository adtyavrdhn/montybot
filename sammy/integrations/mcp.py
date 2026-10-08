"""The user's own MCP servers: kept sealed per user, reached over streamable HTTP, public addresses only.

A server's URL, headers and OAuth tokens are credentials, so they are sealed with the user's data key
(`sammy.crypto`, label `<user>:mcp_server:<id>`): a sealed secret copied onto another user's row, or onto another
of their servers, does not open. Only the name, a slug, the host and the status are in the clear, for lists.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
from cryptography.exceptions import InvalidTag
from psycopg.errors import UniqueViolation
from pydantic import BaseModel
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.mcp import MCPToolset

from sammy import crypto
from sammy.db import Connection
from sammy.integrations import egress, oauth
from sammy.integrations.base import IntegrationError, Tool
from sammy.signins import user_key

Auth = Literal['none', 'headers', 'oauth']
Status = Literal['ready', 'needs_sign_in']
COLUMNS = 'id, user_id, name, slug, host, auth, status'
MAX_RESULT = 30_000
"""Characters of a tool's result the model gets."""


@dataclass(frozen=True, kw_only=True)
class Server:
    id: str
    user_id: str
    name: str
    slug: str
    host: str
    auth: Auth
    status: Status


class Secret(BaseModel):
    url: str
    headers: dict[str, str] = {}
    client: oauth.OAuthClient | None = None
    tokens: oauth.Tokens | None = None


def slug_of(name: str) -> str:
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', ascii_name.lower()).strip('-')[:40] or 'server'


def server_from(row: dict[str, Any]) -> Server:
    return Server(
        id=str(row['id']),
        user_id=str(row['user_id']),
        name=row['name'],
        slug=row['slug'],
        host=row['host'],
        auth=row['auth'],
        status=row['status'],
    )


def label(user_id: str, server_id: str) -> str:
    return f'{user_id}:mcp_server:{server_id}'


# --- storage ---


class NameTaken(Exception):
    pass


async def add(
    connection: Connection, key: bytes, *, user_id: str, name: str, auth: Auth, status: Status, secret: Secret
) -> Server:
    """Call inside a transaction. Raises `NameTaken` if the user has a server of that name already."""
    cursor = await connection.execute('SELECT gen_random_uuid() AS id')
    row = await cursor.fetchone()
    assert row is not None
    server_id = str(row['id'])
    sealed = crypto.seal(
        await user_key(connection, key, user_id), secret.model_dump_json().encode(), label=label(user_id, server_id)
    )
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                'INSERT INTO sammy.mcp_servers (id, user_id, name, slug, host, auth, status, secret) '
                f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}',
                (server_id, user_id, name, slug_of(name), urlsplit(secret.url).hostname or '', auth, status, sealed),
            )
    except UniqueViolation:
        raise NameTaken(name) from None
    row = await cursor.fetchone()
    assert row is not None
    return server_from(row)


async def list_for(connection: Connection, user_id: str) -> list[Server]:
    cursor = await connection.execute(
        f'SELECT {COLUMNS} FROM sammy.mcp_servers WHERE user_id = %s ORDER BY created_at', (user_id,)
    )
    return [server_from(row) for row in await cursor.fetchall()]


async def get(
    connection: Connection, user_id: str, *, server_id: str | None = None, slug: str | None = None
) -> Server | None:
    column, value = ('id', server_id) if server_id is not None else ('slug', slug)
    cursor = await connection.execute(
        f'SELECT {COLUMNS} FROM sammy.mcp_servers WHERE user_id = %s AND {column} = %s', (user_id, value)
    )
    row = await cursor.fetchone()
    return None if row is None else server_from(row)


async def delete(connection: Connection, user_id: str, server_id: str) -> bool:
    cursor = await connection.execute(
        'DELETE FROM sammy.mcp_servers WHERE user_id = %s AND id = %s', (user_id, server_id)
    )
    return cursor.rowcount == 1


async def load_secret(connection: Connection, key: bytes, server: Server, *, lock: bool = False) -> Secret:
    """Call inside a transaction. `lock` holds the row until it ends, so one refresh of its tokens at a time."""
    cursor = await connection.execute(
        f'SELECT secret FROM sammy.mcp_servers WHERE id = %s AND user_id = %s{" FOR UPDATE" if lock else ""}',
        (server.id, server.user_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise IntegrationError(f'{server.name} was removed.')
    try:
        plain = crypto.open_sealed(
            await user_key(connection, key, server.user_id),
            bytes(row['secret']),
            label=label(server.user_id, server.id),
        )
    except (InvalidTag, ValueError) as error:
        raise IntegrationError(
            f'The saved details of {server.name} could not be read. Remove it and add it again.'
        ) from error
    return Secret.model_validate_json(plain)


async def save_secret(connection: Connection, key: bytes, server: Server, secret: Secret, status: Status) -> None:
    """Call inside a transaction."""
    sealed = crypto.seal(
        await user_key(connection, key, server.user_id),
        secret.model_dump_json().encode(),
        label=label(server.user_id, server.id),
    )
    await connection.execute(
        'UPDATE sammy.mcp_servers SET secret = %s, status = %s, updated_at = now() WHERE id = %s AND user_id = %s',
        (sealed, status, server.id, server.user_id),
    )


async def start_flow(connection: Connection, key: bytes, server: Server, state: str, verifier: str) -> None:
    """Call inside a transaction. A sign-in has ten minutes; older ones of the server are dropped."""
    await connection.execute(
        'DELETE FROM sammy.mcp_oauth_flows WHERE server_id = %s OR expires_at < now()', (server.id,)
    )
    sealed = crypto.seal(
        await user_key(connection, key, server.user_id), verifier.encode(), label=f'{server.user_id}:mcp_oauth:{state}'
    )
    await connection.execute(
        'INSERT INTO sammy.mcp_oauth_flows (state, user_id, server_id, verifier, expires_at) '
        "VALUES (%s, %s, %s, %s, now() + interval '10 minutes')",
        (state, server.user_id, server.id, sealed),
    )


async def take_flow(connection: Connection, key: bytes, state: str) -> tuple[Server, str] | None:
    """The server and PKCE verifier of a sign-in that is still on, used up. Call inside a transaction."""
    cursor = await connection.execute(
        'DELETE FROM sammy.mcp_oauth_flows WHERE state = %s AND expires_at > now() RETURNING user_id, server_id, verifier',
        (state,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    user_id = str(row['user_id'])
    server = await get(connection, user_id, server_id=str(row['server_id']))
    if server is None:
        return None
    verifier = crypto.open_sealed(
        await user_key(connection, key, user_id), bytes(row['verifier']), label=f'{user_id}:mcp_oauth:{state}'
    )
    return server, verifier.decode()


# --- talking to a server ---


def headers_of(secret: Secret) -> dict[str, str]:
    if secret.tokens is not None:
        return {**secret.headers, 'Authorization': f'Bearer {secret.tokens.access_token}'}
    return dict(secret.headers)


@asynccontextmanager
async def session(secret: Secret, *, allow_private: bool) -> AsyncGenerator[MCPToolset[Any]]:
    """An open session with the server. Errors on the way in or out become `IntegrationError`s."""
    # Handed over unopened: the MCP client opens it, and it is closed here once the session is over.
    http = egress.public_client(allow_private=allow_private, headers=headers_of(secret))
    toolset = MCPToolset[Any](secret.url, http_client=http, tool_error_behavior='failed', init_timeout=20)
    try:
        async with toolset:
            yield toolset
    except (IntegrationError, ToolFailed, ModelRetry):
        raise
    except Exception as error:  # the MCP client raises many kinds: HTTP, protocol, groups of them
        raise IntegrationError(
            f'The server could not be reached or did not answer as an MCP server ({type(error).__name__}).'
        ) from error
    finally:
        await http.aclose()


async def list_tools(secret: Secret, *, allow_private: bool) -> list[Tool]:
    async with session(secret, allow_private=allow_private) as toolset:
        found = await toolset.list_tools()
    return [
        Tool(
            name=tool.name,
            title=tool.title or (tool.annotations.title if tool.annotations and tool.annotations.title else tool.name),
            description=tool.description or '',
            parameters=tool.input_schema,
            read_only=bool(tool.annotations and tool.annotations.read_only_hint),
        )
        for tool in found
    ]


async def call_tool(secret: Secret, name: str, arguments: dict[str, Any], *, allow_private: bool) -> str:
    async with session(secret, allow_private=allow_private) as toolset:
        try:
            result = await toolset.direct_call_tool(name, arguments)
        except (ToolFailed, ModelRetry) as error:
            raise IntegrationError(str(error)) from None
    return result_text(result)


def result_text(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + '\n[cut: the result was longer]'


async def probe(url: str, headers: dict[str, str], *, allow_private: bool) -> httpx2.Response | None:
    """The server's 401/403 if it wants a sign-in first; None if it answers. Raises `IntegrationError` if it cannot
    be reached."""
    async with egress.public_client(allow_private=allow_private, headers=headers) as http:
        try:
            return await oauth.needs_sign_in(http, url)
        except httpx2.HTTPError as error:
            raise IntegrationError(f'The server could not be reached: {error}') from error
