"""Stand-ins for the services integrations reach, each on 127.0.0.1 with a port of its own:

- `FakeComposio`: the parts of Composio's REST API Sammy uses (`sammy.integrations.composio`), with the
  multi-tenant behaviour that matters: accounts belong to a `user_id`, a tool runs only on an active account of the
  user it runs for, and the project holds another app's user and auth config too. Its pages are two items long, so
  every list is paged. "Signing in" is opening the link: the account turns active and the browser goes to the
  callback, as Composio's hosted page does.
- `NotesServer`: an MCP server (the MCP SDK's own) with a read-only `list_notes` and a `add_note` that changes
  something. Open, behind a fixed bearer token, or behind OAuth from the SDK's own authorization server: discovery,
  dynamic client registration, PKCE and tokens as the spec has them, with a consent that says yes at once.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import uvicorn
from helpers import eventually, free_port
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY = 'fake-composio-key'
OTHER_APP_USER = 'T04M32PUL9M:U0A9MK7K93K'
"""Another app's user in the same Composio project, with a Linear account of their own."""
NOTES_TOKEN = 'notes-token'


class Served:
    """An ASGI app served by uvicorn on a thread."""

    def __init__(self, app: ASGIApp, port: int | None = None) -> None:
        self.port = port or free_port()
        self._server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning'))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def start(self) -> None:
        self._thread.start()
        eventually(lambda: self._server.started or None, what='the fake service to start')

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(10)


# --- Composio ---

TOOLKITS = [
    {'slug': 'linear', 'name': 'Linear', 'auth_schemes': ['OAUTH2'], 'composio_managed_auth_schemes': ['OAUTH2'],
     'meta': {'logo': 'https://logos.composio.dev/api/linear', 'description': 'Issue tracking',
              'categories': [{'id': 'pm', 'name': 'project management'}]}},
    {'slug': 'github', 'name': 'GitHub', 'auth_schemes': ['OAUTH2'], 'composio_managed_auth_schemes': ['OAUTH2'],
     'meta': {'logo': 'https://logos.composio.dev/api/github', 'description': 'Code hosting', 'categories': []}},
    {'slug': 'gmail', 'name': 'Gmail', 'auth_schemes': ['OAUTH2'], 'composio_managed_auth_schemes': ['OAUTH2'],
     'meta': {'logo': '', 'description': 'Email', 'categories': []}},
    # Not offered: it needs the deployment to set up its own credentials.
    {'slug': 'acme_crm', 'name': 'Acme CRM', 'auth_schemes': ['API_KEY'], 'composio_managed_auth_schemes': [],
     'meta': {'logo': '', 'description': 'A CRM', 'categories': []}},
]  # fmt: skip

LINEAR_TOOLS = [
    {'slug': 'LINEAR_LIST_LINEAR_ISSUES', 'name': 'List issues', 'description': 'List the issues assigned to you.',
     'input_parameters': {'type': 'object', 'properties': {}}, 'tags': ['readOnlyHint', 'important'],
     'version': '20260924_00', 'toolkit': {'slug': 'linear'}},
    {'slug': 'LINEAR_CREATE_LINEAR_ISSUE', 'name': 'Create issue', 'description': 'Create a new issue.',
     'input_parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']},
     'tags': ['createHint'], 'version': '20260924_00', 'toolkit': {'slug': 'linear'}},
    {'slug': 'LINEAR_OLD_TOOL', 'name': 'Old', 'description': 'Gone.', 'input_parameters': {}, 'tags': ['deprecated'],
     'version': '1', 'toolkit': {'slug': 'linear'}},
]  # fmt: skip


@dataclass
class Account:
    id: str
    user_id: str
    toolkit: str
    auth_config_id: str
    status: str
    callback_url: str = ''


@dataclass
class FakeComposio:
    accounts: dict[str, Account] = field(default_factory=dict[str, Account])
    auth_configs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    links: dict[str, str] = field(default_factory=dict[str, str])
    executed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    issues: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """Each account's Linear issues."""
    _served: Served | None = None

    def __post_init__(self) -> None:
        # Another app shares the project: its auth config for Linear, and its user's active account.
        self.auth_configs.append(self._auth_config('linear', 'viktor-linear'))
        other = Account(
            id='ca_other', user_id=OTHER_APP_USER, toolkit='linear', auth_config_id='ac_viktor', status='ACTIVE'
        )
        self.accounts[other.id] = other
        self.issues[other.id] = ['Somebody else’s secret issue']

    @property
    def url(self) -> str:
        assert self._served is not None
        return self._served.url

    def start(self) -> None:
        self._served = Served(self.app())
        self._served.start()

    def stop(self) -> None:
        if self._served is not None:
            self._served.stop()

    def _auth_config(self, toolkit: str, name: str) -> dict[str, Any]:
        return {'id': f'ac_{secrets.token_hex(4)}', 'name': name, 'status': 'ENABLED', 'toolkit': {'slug': toolkit},
                'created_at': f'2026-01-01T00:00:{len(self.auth_configs):02d}Z'}  # fmt: skip

    def accounts_of(self, user_id: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.user_id == user_id]

    def app(self) -> Starlette:
        def page(items: list[Any], request: Request) -> JSONResponse:
            start = int(request.query_params.get('cursor') or 0)
            more = start + 2 < len(items)
            return JSONResponse({'items': items[start : start + 2], 'next_cursor': str(start + 2) if more else None})

        def error(status: int, message: str) -> JSONResponse:
            return JSONResponse({'error': {'message': message, 'status': status}}, status_code=status)

        def account_json(a: Account) -> dict[str, Any]:
            return {'id': a.id, 'user_id': a.user_id, 'status': a.status, 'toolkit': {'slug': a.toolkit},
                    'auth_config': {'id': a.auth_config_id}, 'data': {'access_token': 'a-real-token'}}  # fmt: skip

        async def toolkits(request: Request) -> Response:
            return page(TOOLKITS, request)

        async def auth_configs(request: Request) -> Response:
            if request.method == 'POST':
                body = await request.json()
                assert body['auth_config']['type'] == 'use_composio_managed_auth'
                made = self._auth_config(body['toolkit']['slug'], body['auth_config']['name'])
                self.auth_configs.append(made)
                return JSONResponse({'toolkit': made['toolkit'], 'auth_config': {'id': made['id']}}, status_code=201)
            slug = request.query_params.get('toolkit_slug')
            return page([c for c in self.auth_configs if c['toolkit']['slug'] == slug], request)

        async def link(request: Request) -> Response:
            body = await request.json()
            config = next((c for c in self.auth_configs if c['id'] == body['auth_config_id']), None)
            if config is None:
                return error(400, 'no such auth config')
            account = Account(
                id=f'ca_{secrets.token_hex(4)}', user_id=body['user_id'], toolkit=config['toolkit']['slug'],
                auth_config_id=config['id'], status='INITIALIZING', callback_url=body['callback_url'],
            )  # fmt: skip
            self.accounts[account.id] = account
            token = f'lk_{secrets.token_hex(4)}'
            self.links[token] = account.id
            return JSONResponse({'redirect_url': f'{self.url}/link/{token}', 'connected_account_id': account.id,
                                 'link_token': token, 'expires_at': '2099-01-01T00:00:00Z'}, status_code=201)  # fmt: skip

        async def sign_in(request: Request) -> Response:
            """The user signs in to the app on Composio's page: the account is active, and back they go."""
            account = self.accounts[self.links.pop(request.path_params['token'])]
            account.status = 'ACTIVE'
            self.issues.setdefault(account.id, ['Fix the login page'])
            query = urlencode({'status': 'success', 'connected_account_id': account.id})
            return RedirectResponse(f'{account.callback_url}&{query}', status_code=303)

        async def list_accounts(request: Request) -> Response:
            users = request.query_params.get('user_ids', '').split(',')
            return page([account_json(a) for a in self.accounts.values() if a.user_id in users], request)

        async def one_account(request: Request) -> Response:
            account = self.accounts.get(request.path_params['id'])
            if account is None:
                return error(404, 'Connected account not found')
            if request.method == 'DELETE':
                del self.accounts[account.id]
                return JSONResponse({'success': True})
            return JSONResponse(account_json(account))

        async def tools(request: Request) -> Response:
            slug = request.query_params.get('toolkit_slug')
            return page(LINEAR_TOOLS if slug == 'linear' else [], request)

        async def execute(request: Request) -> Response:
            body = await request.json()
            self.executed.append({'tool': request.path_params['slug'], **body})
            tool = next((t for t in LINEAR_TOOLS if t['slug'] == request.path_params['slug']), None)
            account = self.accounts.get(body.get('connected_account_id', ''))
            if tool is None:
                return error(404, 'no such tool')
            if account is None or account.user_id != body.get('user_id') or account.status != 'ACTIVE':
                return error(400, 'no active connected account for this user')
            if body.get('version') != tool['version']:
                return error(400, 'unknown version')
            issues = self.issues.setdefault(account.id, [])
            if tool['slug'] == 'LINEAR_CREATE_LINEAR_ISSUE':
                issues.append(body['arguments']['title'])
                return JSONResponse(
                    {'successful': True, 'data': {'created': body['arguments']['title']}, 'error': None}
                )
            return JSONResponse({'successful': True, 'data': {'issues': issues}, 'error': None})

        def keyed(handler: Any) -> Any:
            async def endpoint(request: Request) -> Response:
                if request.headers.get('x-api-key') != API_KEY:
                    return error(401, 'invalid api key')
                return await handler(request)

            return endpoint

        api = '/api/v3.1'
        return Starlette(
            routes=[
                Route(f'{api}/toolkits', keyed(toolkits)),
                Route(f'{api}/auth_configs', keyed(auth_configs), methods=['GET', 'POST']),
                Route(f'{api}/connected_accounts/link', keyed(link), methods=['POST']),
                Route(f'{api}/connected_accounts', keyed(list_accounts)),
                Route(f'{api}/connected_accounts/{{id}}', keyed(one_account), methods=['GET', 'DELETE']),
                Route(f'{api}/tools', keyed(tools)),
                Route(f'{api}/tools/execute/{{slug}}', keyed(execute), methods=['POST']),
                Route('/link/{token}', sign_in),
            ]
        )


# --- an MCP server ---


class Consenting:
    """An OAuth authorization server, in memory, whose user says yes to every client at once."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.token_seconds = 3600

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id or '',
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
        )  # fmt: skip
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        expires = int(time.time()) + self.token_seconds
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=expires, resource=resource
        )
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(
            access_token=access, expires_in=self.token_seconds, refresh_token=refresh, scope=' '.join(scopes) or None
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        del self.codes[authorization_code.code]
        return self._issue(authorization_code.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        del self.refresh[refresh_token.token]
        return self._issue(refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.access.get(token)
        return found if found is not None and (found.expires_at or 0) > time.time() else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    async def exchange_identity_assertion(self, *args: Any, **kwargs: Any) -> OAuthToken:
        raise NotImplementedError


class BearerOnly:
    """Lets requests through only with the server's one token, as a server keyed by a header does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http' and dict(scope['headers']).get(b'authorization') != f'Bearer {NOTES_TOKEN}'.encode():
            await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class NotesServer:
    """An MCP server at `<url>/mcp`, open, behind a token (`NOTES_TOKEN`), or behind OAuth."""

    def __init__(self, auth: Literal['open', 'token', 'oauth'] = 'open') -> None:
        self.notes = ['Buy oat milk']
        self.calls: list[str] = []
        self.auth = auth
        self.consent = Consenting()
        self._served: Served | None = None

    @property
    def url(self) -> str:
        assert self._served is not None
        return self._served.url

    @property
    def mcp_url(self) -> str:
        return f'{self.url}/mcp'

    def start(self) -> None:
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        oauth = self.auth == 'oauth'
        server = MCPServer(
            'notes',
            auth_server_provider=self.consent if oauth else None,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(base),
                resource_server_url=AnyHttpUrl(f'{base}/mcp'),
                client_registration_options=ClientRegistrationOptions(enabled=True),
                validate_token_resource=True,  # a token must be for this server: Sammy must ask for it (RFC 8707)
            )
            if oauth
            else None,
        )

        @server.tool(annotations=ToolAnnotations(read_only_hint=True))
        def list_notes() -> list[str]:
            """The user's notes."""
            self.calls.append('list_notes')
            return self.notes

        @server.tool()
        def add_note(text: str) -> str:
            """Add a note."""
            self.calls.append('add_note')
            self.notes.append(text)
            return 'Added.'

        app: ASGIApp = server.streamable_http_app(host='127.0.0.1')
        if self.auth == 'token':
            app = BearerOnly(app)
        self._served = Served(app, port)  # the address is in its OAuth metadata, so it is chosen first
        self._served.start()

    def stop(self) -> None:
        if self._served is not None:
            self._served.stop()
