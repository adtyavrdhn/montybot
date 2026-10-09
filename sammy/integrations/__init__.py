"""Integrations: the services a user connects so Sammy can work in them. Two kinds, one list:

- **Apps through Composio** (`composio.py`): Gmail, Slack, Jira... one click, Composio signs the user in.
  Their key is the app's slug: `gmail`.
- **The user's own MCP servers** (`mcp.py`): a URL, with a header or an OAuth sign-in (`oauth.py`). Their key is
  `mcp:<slug of its name>`. The hosted servers of the services pydantic-ai-harness integrates (Linear, Notion,
  GitHub...) are listed (`catalog.py`), and added in a click or with a pasted token.

```
web app / Mac app                     sammy.api /api/integrations...      Integrations (one per process)
  Integrations page: list, add, remove  ----------------------------------->  connections, connect_link, add_server...
  chat: "Connect Linear" card           POST /api/asks/<id> or the callback   (sammy.approvals.connected wakes runs)
agent (sammy.integration_tools)    each call is one DBOS step             tools, call
```

Every method takes the user id from its caller (the signed-in user, or the run's user) and acts on that user's
connections only. Nothing here takes a user from the model or from a request body.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal

from itsdangerous import BadSignature, URLSafeTimedSerializer

from sammy.db import Pool
from sammy.integrations import catalog, egress, mcp, oauth
from sammy.integrations.base import IntegrationError, Tool
from sammy.integrations.catalog import normalized
from sammy.integrations.composio import Composio, Toolkit
from sammy.settings import Settings

__all__ = ['Connection', 'IntegrationError', 'Integrations', 'Offer', 'Tool', 'Toolkit']

MCP_PREFIX = 'mcp:'
COMPOSIO_CALLBACK = '/integrations/composio/callback'
MCP_CALLBACK = '/integrations/mcp/callback'
LINK_SECONDS = 60 * 60

Provider = Literal['composio', 'mcp']
State = Literal['connected', 'needs_sign_in', 'broken']


@dataclass(frozen=True, kw_only=True)
class Connection:
    id: str
    """Composio's account id, or the MCP server's id: what removing it names."""
    key: str
    """What the agent names it by: `linear`, `mcp:notes`."""
    provider: Provider
    name: str
    detail: str
    """The app's one-line description, or the MCP server's host."""
    logo: str
    state: State

    def json(self) -> dict[str, str]:
        return {
            'id': self.id,
            'key': self.key,
            'provider': self.provider,
            'name': self.name,
            'detail': self.detail,
            'logo': self.logo,
            'state': self.state,
        }


@dataclass(frozen=True, kw_only=True)
class Offer:
    """What a chat offers the user to connect for a service the model named: an app Composio can connect, or (when
    there is none) their own MCP server."""

    provider: Provider
    key: str
    """The app's slug, or the listed MCP server's key (`posthog`), or empty for an MCP server the user has yet to
    find and add."""
    name: str
    logo: str = ''
    url: str = ''
    """A listed MCP server's address (`catalog.FEATURED`): the chat adds it, and the user signs in, in one click."""
    auth: str = ''
    """For a listed MCP server: `oauth`, or `token` for one the user pastes a token for, as `token_hint` says,
    sent in `token_header` as `Bearer <token>`."""
    token_hint: str = ''
    token_header: str = ''

    @classmethod
    def of(cls, listed: catalog.Listed) -> Offer:
        token = listed.needs_token
        return cls(
            provider='mcp',
            key=listed.key,
            name=listed.name,
            logo=listed.logo,
            url=listed.url,
            auth=listed.auth,
            token_hint=listed.token_hint if token else '',
            token_header=listed.token_header if token else '',
        )

    def json(self) -> dict[str, str]:
        shown = {'provider': self.provider, 'key': self.key, 'name': self.name, 'logo': self.logo}
        optional = {
            'url': self.url,
            'auth': self.auth,
            'token_hint': self.token_hint,
            'token_header': self.token_header,
        }
        return shown | {key: value for key, value in optional.items() if value}


class Integrations:
    def __init__(self, pool: Pool, deployment_key: bytes, settings: Settings) -> None:
        self._pool = pool
        self._key = deployment_key
        self._settings = settings
        self._allow_private = settings.allow_private_networks
        self._signer = URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt='sammy.integrations')
        api_key = settings.composio_api_key
        self.composio = (
            Composio(
                api_key=api_key.get_secret_value(),
                base_url=settings.composio_url,
                user_prefix=settings.composio_user_prefix,
            )
            if api_key is not None
            else None
        )

    async def aclose(self) -> None:
        if self.composio is not None:
            await self.composio.aclose()

    # --- what the user has ---

    async def connections(self, user_id: str) -> list[Connection]:
        found: list[Connection] = []
        if self.composio is not None:
            catalog = await self.composio.catalog()
            for account in await self.composio.accounts(user_id):
                app = catalog.get(account.toolkit)
                found.append(
                    Connection(
                        id=account.id,
                        key=account.toolkit,
                        provider='composio',
                        name=app.name if app else account.toolkit,
                        detail=app.description if app else '',
                        logo=app.logo if app else '',
                        state='connected' if account.status == 'ACTIVE' else 'broken',
                    )
                )
        async with self._pool.connection() as connection:
            servers = await mcp.list_for(connection, user_id)
        found.extend(
            Connection(
                id=server.id,
                key=MCP_PREFIX + server.slug,
                provider='mcp',
                name=server.name,
                detail=server.host,
                logo='',
                state='connected' if server.status == 'ready' else 'needs_sign_in',
            )
            for server in servers
        )
        return found

    async def catalog(self) -> list[Toolkit]:
        if self.composio is None:
            return []
        return sorted((await self.composio.catalog()).values(), key=lambda app: app.name.lower())

    async def listing(self) -> list[dict[str, object]]:
        """What the Integrations page lists (`catalog.entries`): MCP servers there even without Composio."""
        return catalog.entries(await self.composio.catalog() if self.composio is not None else {})

    async def offer(self, user_id: str, service: str) -> Connection | Offer:
        """For a service the model named ("linear", "Linear issues"): the user's connection to it if they have one,
        else what they can connect for it."""
        wanted = normalized(service.removeprefix(MCP_PREFIX))
        connections = await self.connections(user_id)
        for connection in connections:
            if wanted in (normalized(connection.key.removeprefix(MCP_PREFIX)), normalized(connection.name)):
                return connection
        if (preset := catalog.mcp_preset(service)) is not None:
            # Added already, under a name of the user's own (the same server, whatever it is called), or connected
            # through Composio's app for the service before its own server was listed.
            for connection in connections:
                if (connection.provider == 'mcp' and connection.detail == preset.host) or (
                    connection.provider == 'composio' and connection.key == preset.key
                ):
                    return connection
            return Offer.of(preset)
        if self.composio is not None:
            apps = await self.composio.catalog()
            exact = next((a for a in apps.values() if wanted in (normalized(a.slug), normalized(a.name))), None)
            # "Linear issues" is Linear: the longest app name the words start with, of three letters or more.
            starts = [
                a for a in apps.values() if len(normalized(a.name)) >= 3 and wanted.startswith(normalized(a.name))
            ]
            app = exact or max(starts, key=lambda a: len(normalized(a.name)), default=None)
            if app is not None:
                return Offer(provider='composio', key=app.slug, name=app.name, logo=app.logo)
        return Offer(provider='mcp', key='', name=service.strip()[:80])

    # --- the agent's use ---

    async def tools(self, user_id: str, key: str) -> list[Tool]:
        if key.startswith(MCP_PREFIX):
            return await mcp.list_tools(await self._secret(user_id, key), allow_private=self._allow_private)
        if self.composio is None or await self.composio.active_account(user_id, key) is None:
            raise IntegrationError(f'{key} is not connected.')
        return await self.composio.tools(key)

    async def tool(self, user_id: str, key: str, name: str) -> Tool:
        tool = next((t for t in await self.tools(user_id, key) if t.name == name), None)
        if tool is None:
            raise IntegrationError(f'{key} has no tool {name!r}. List its tools first.')
        return tool

    async def call(self, user_id: str, key: str, tool: Tool, arguments: dict[str, Any]) -> str:
        if key.startswith(MCP_PREFIX):
            secret = await self._secret(user_id, key)
            return await mcp.call_tool(secret, tool.name, arguments, allow_private=self._allow_private)
        account = None if self.composio is None else await self.composio.active_account(user_id, key)
        if self.composio is None or account is None:
            raise IntegrationError(f'{key} is not connected.')
        return mcp.result_text(await self.composio.execute(user_id, account, tool, arguments))

    async def _secret(self, user_id: str, key: str) -> mcp.Secret:
        """The server's secret, its OAuth tokens refreshed first if they are about to expire."""
        async with self._pool.connection() as connection, connection.transaction():
            server = await mcp.get(connection, user_id, slug=key.removeprefix(MCP_PREFIX))
            if server is None:
                raise IntegrationError(f'{key} is not connected.')
            if server.status != 'ready':
                raise IntegrationError(f'{server.name} needs the user to sign in again.')
            secret = await mcp.load_secret(connection, self._key, server, lock=True)
            if secret.client is None or secret.tokens is None or not secret.tokens.expiring:
                return secret
            async with egress.public_client(allow_private=self._allow_private) as http:
                tokens = await oauth.refresh(http, secret.client, secret.tokens)
            if tokens is None:
                await mcp.save_secret(connection, self._key, server, secret, 'needs_sign_in')
            else:
                secret = secret.model_copy(update={'tokens': tokens})
                await mcp.save_secret(connection, self._key, server, secret, 'ready')
        if tokens is None:
            raise IntegrationError(f'The sign-in to {server.name} has expired: the user needs to sign in again.')
        return secret

    # --- Composio apps ---

    async def connect_link(self, user_id: str, toolkit: str) -> str:
        if self.composio is None:
            raise IntegrationError('Apps cannot be connected on this server.')
        state = self._signer.dumps({'u': user_id, 't': toolkit})
        return await self.composio.connect_link(
            user_id, toolkit, f'{self._settings.public_url}{COMPOSIO_CALLBACK}?state={state}'
        )

    async def composio_returned(self, state: str) -> tuple[str, str, bool]:
        """Composio sent the user back: whose app it was, which app, and whether it is connected now. Only the signed
        state counts (it names the user and the app); what Composio adds to the URL is not trusted."""
        if self.composio is None:
            raise IntegrationError('Apps cannot be connected on this server.')
        try:
            payload = self._signer.loads(state, max_age=LINK_SECONDS)
        except BadSignature:
            raise IntegrationError('That link is not valid any more. Start connecting again from Sammy.') from None
        user_id, toolkit = str(payload['u']), str(payload['t'])
        return user_id, toolkit, await self.composio.active_account(user_id, toolkit) is not None

    async def disconnect(self, user_id: str, account_id: str) -> bool:
        return self.composio is not None and await self.composio.disconnect(user_id, account_id)

    async def disconnect_all(self, user_id: str) -> None:
        """Every app the user connected, when their account is deleted. Their MCP servers are rows of ours, which go
        with the account (sammy.accounts)."""
        if self.composio is not None:
            await self.composio.disconnect_all(user_id)

    # --- the user's MCP servers ---

    async def add_server(
        self, user_id: str, *, name: str, url: str, headers: dict[str, str]
    ) -> tuple[Connection, str | None]:
        """Add the server, and check it answers. Returns it, and where the user signs in if it wants an OAuth sign-in
        first. Raises `IntegrationError` (nothing is kept) if it cannot be used, `mcp.NameTaken` for a name in use."""
        url = egress.check_url(url, allow_private=self._allow_private)
        unauthorized = await mcp.probe(url, headers, allow_private=self._allow_private)
        secret = mcp.Secret(url=url, headers=headers)
        if unauthorized is None:
            await mcp.list_tools(secret, allow_private=self._allow_private)  # it is an MCP server, and it works
            auth: mcp.Auth = 'headers' if headers else 'none'
        elif headers:
            raise IntegrationError('The server refused those credentials.')
        else:
            async with egress.public_client(allow_private=self._allow_private) as http:
                secret.client = await oauth.register(http, url, unauthorized, self._redirect_uri)
            auth = 'oauth'
        async with self._pool.connection() as connection, connection.transaction():
            server = await mcp.add(
                connection,
                self._key,
                user_id=user_id,
                name=name,
                auth=auth,
                status='ready' if auth != 'oauth' else 'needs_sign_in',
                secret=secret,
            )
        sign_in = await self.sign_in_link(user_id, server.id) if auth == 'oauth' else None
        return self._connection_of(server), sign_in

    async def sign_in_link(self, user_id: str, server_id: str) -> str | None:
        """Where the user signs in to their server (again). None if it is not theirs or has no OAuth sign-in."""
        state, verifier = secrets.token_urlsafe(32), oauth.new_verifier()
        async with self._pool.connection() as connection, connection.transaction():
            server = await mcp.get(connection, user_id, server_id=server_id)
            if server is None or server.auth != 'oauth':
                return None
            secret = await mcp.load_secret(connection, self._key, server)
            assert secret.client is not None
            await mcp.start_flow(connection, self._key, server, state, verifier)
        return oauth.authorize_url(secret.client, state=state, verifier=verifier, redirect_uri=self._redirect_uri)

    async def server_signed_in(self, state: str, code: str, iss: str | None) -> tuple[str, Connection]:
        """The OAuth server sent the user back with a code. The state names the sign-in, its user and server: returns
        that user's id and the server, now ready."""
        async with self._pool.connection() as connection, connection.transaction():
            taken = await mcp.take_flow(connection, self._key, state)
            if taken is None:
                raise IntegrationError('That sign-in has expired or was used already. Start it again from Sammy.')
            server, verifier = taken
            secret = await mcp.load_secret(connection, self._key, server)
        assert secret.client is not None
        oauth.check_issuer(secret.client, iss)
        async with egress.public_client(allow_private=self._allow_private) as http:
            tokens = await oauth.exchange(
                http, secret.client, code=code, verifier=verifier, redirect_uri=self._redirect_uri
            )
        secret = secret.model_copy(update={'tokens': tokens})
        await mcp.list_tools(secret, allow_private=self._allow_private)  # the tokens work
        async with self._pool.connection() as connection, connection.transaction():
            await mcp.save_secret(connection, self._key, server, secret, 'ready')
        return server.user_id, self._connection_of(server, 'ready')

    async def remove_server(self, user_id: str, server_id: str) -> bool:
        async with self._pool.connection() as connection:
            return await mcp.delete(connection, user_id, server_id)

    @property
    def _redirect_uri(self) -> str:
        return f'{self._settings.public_url}{MCP_CALLBACK}'

    @staticmethod
    def _connection_of(server: mcp.Server, status: mcp.Status | None = None) -> Connection:
        return Connection(
            id=server.id,
            key=MCP_PREFIX + server.slug,
            provider='mcp',
            name=server.name,
            detail=server.host,
            logo='',
            state='connected' if (status or server.status) == 'ready' else 'needs_sign_in',
        )
