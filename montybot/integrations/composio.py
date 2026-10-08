"""Composio: one-click connections to apps such as Linear, GitHub and Gmail, for every user from one Composio project.

```
user clicks Connect Linear
  connect_link(user, 'linear')       our auth config `montybot-linear` (Composio-managed OAuth), made on first use
    POST connected_accounts/link     -> connect.composio.dev/link/...: the user signs in to Linear there
  Composio -> our callback           (montybot.integrations.connect_callback), then the user's account is ACTIVE
agent: call_integration_tool('linear', 'LINEAR_CREATE_LINEAR_ISSUE', {...})
  execute(user, tool)                POST tools/execute/<tool> with the user's id and their own account's id
```

**One project, many users.** The deployment's API key reaches every connection in the project, other apps' too (the
project can be shared). So:

- Each Monty user is `<COMPOSIO_USER_PREFIX><user id>` in Composio (`composio_user`). It is made here from the user
  id the caller already holds (the signed-in user, or the run's), never taken from a request or from the model.
- Listing asks Composio for that one user's accounts, and drops anything that comes back for someone else.
- Removing an account fetches it first and refuses one that is not that user's.
- A tool runs with the user's id *and* the id of their own active account for the tool's app, so Composio cannot
  pick another account (a SHARED one, say) for it. Accounts are made PRIVATE, Composio's default.
- Composio's account records carry token fields. Only the fields below leave this module, and none of it is logged.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from montybot.integrations.base import IntegrationError, Tool

API = '/api/v3.1'
AUTH_CONFIG_PREFIX = 'montybot-'
"""Our auth configs are `montybot-<toolkit>`: other apps' in the same project are left alone."""
CATALOG_SECONDS = 60 * 60
PAGE = 1000


class ComposioError(IntegrationError):
    """Composio refused or failed."""


@dataclass(frozen=True, kw_only=True)
class Toolkit:
    slug: str
    name: str
    logo: str
    description: str
    categories: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Account:
    id: str
    toolkit: str
    status: str
    """`ACTIVE` once connected; `INITIALIZING` or `INITIATED` while the user signs in; `EXPIRED`, `FAILED`..."""


class Composio:
    def __init__(self, *, api_key: str, base_url: str, user_prefix: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, headers={'x-api-key': api_key}, timeout=30)
        self._prefix = user_prefix
        self._catalog: tuple[float, dict[str, Toolkit]] | None = None
        self._tools: dict[str, tuple[float, list[Tool]]] = {}
        self._auth_configs: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def composio_user(self, user_id: str) -> str:
        return f'{self._prefix}{user_id}'

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, f'{API}/{path}', **kwargs)
        except httpx.HTTPError as error:
            raise ComposioError(f'Composio could not be reached ({type(error).__name__})') from None
        if response.status_code == 404:
            return None
        if response.is_error:
            raise ComposioError(f'Composio answered {response.status_code}: {error_message(response)}')
        return response.json() if response.content else {}

    async def _pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self._request(
                'GET', path, params={**params, 'limit': PAGE, **({'cursor': cursor} if cursor else {})}
            )
            items.extend(page['items'])
            cursor = page.get('next_cursor')
            if not cursor:
                return items

    # --- the catalog: every app a user can connect with one click ---

    async def catalog(self) -> dict[str, Toolkit]:
        """The apps Composio signs users in to itself (Composio-managed OAuth), by slug: nothing to set up per app."""
        async with self._lock:
            if self._catalog is None or time.monotonic() - self._catalog[0] > CATALOG_SECONDS:
                found = {
                    item['slug']: toolkit_from(item)
                    for item in await self._pages('toolkits', {})
                    if item.get('composio_managed_auth_schemes') and not item.get('is_local_toolkit')
                }
                self._catalog = (time.monotonic(), found)
            return self._catalog[1]

    # --- a user's accounts ---

    async def accounts(self, user_id: str) -> list[Account]:
        """The user's accounts that are connected or broken; not ones still being signed in to, and not a broken one
        of an app they have connected again since."""
        who = self.composio_user(user_id)
        items = await self._pages('connected_accounts', {'user_ids': who})
        found = [
            Account(id=item['id'], toolkit=item['toolkit']['slug'], status=item['status'])
            for item in items
            if item.get('user_id') == who and item['status'] not in ('INITIALIZING', 'INITIATED')
        ]
        active = {a.toolkit for a in found if a.status == 'ACTIVE'}
        return [a for a in found if a.status == 'ACTIVE' or a.toolkit not in active]

    async def active_account(self, user_id: str, toolkit: str) -> Account | None:
        return next((a for a in await self.accounts(user_id) if a.toolkit == toolkit and a.status == 'ACTIVE'), None)

    async def connect_link(self, user_id: str, toolkit: str, callback_url: str) -> str:
        """Where the user signs in to `toolkit`, for a new account of theirs. Composio sends them to `callback_url`
        afterwards."""
        if toolkit not in await self.catalog():
            raise ComposioError(f'{toolkit} cannot be connected')
        link = await self._request(
            'POST',
            'connected_accounts/link',
            json={
                'auth_config_id': await self._auth_config(toolkit),
                'user_id': self.composio_user(user_id),
                'callback_url': callback_url,
            },
        )
        return str(link['redirect_url'])

    async def disconnect(self, user_id: str, account_id: str) -> bool:
        """Remove one of the user's accounts. False if there is no such account of theirs."""
        account = await self._request('GET', f'connected_accounts/{account_id}')
        if account is None or account.get('user_id') != self.composio_user(user_id):
            return False
        await self._request('DELETE', f'connected_accounts/{account_id}')
        return True

    async def _auth_config(self, toolkit: str) -> str:
        """Our auth config for `toolkit`, made (Composio-managed OAuth) the first time anyone connects it."""
        name = f'{AUTH_CONFIG_PREFIX}{toolkit}'
        async with self._lock:
            if toolkit not in self._auth_configs:
                found = [
                    item
                    for item in await self._pages('auth_configs', {'toolkit_slug': toolkit})
                    if item.get('name') == name and item.get('status') == 'ENABLED'
                ]
                if found:  # the oldest, so every server picks the same one
                    self._auth_configs[toolkit] = min(found, key=lambda item: item['created_at'])['id']
                else:
                    made = await self._request(
                        'POST',
                        'auth_configs',
                        json={
                            'toolkit': {'slug': toolkit},
                            'auth_config': {'type': 'use_composio_managed_auth', 'name': name},
                        },
                    )
                    self._auth_configs[toolkit] = made['auth_config']['id']
            return self._auth_configs[toolkit]

    # --- tools ---

    async def tools(self, toolkit: str) -> list[Tool]:
        async with self._lock:
            cached = self._tools.get(toolkit)
        if cached is not None and time.monotonic() - cached[0] < CATALOG_SECONDS:
            return cached[1]
        tools = [
            tool_from(item)
            for item in await self._pages('tools', {'toolkit_slug': toolkit})
            if not item.get('is_deprecated') and 'deprecated' not in item.get('tags', [])
        ]
        async with self._lock:
            self._tools[toolkit] = (time.monotonic(), tools)
        return tools

    async def execute(self, user_id: str, account: Account, tool: Tool, arguments: dict[str, Any]) -> Any:
        """Run `tool` as the user, on `account`, which must be theirs (from `active_account`)."""
        result = await self._request(
            'POST',
            f'tools/execute/{tool.name}',
            json={
                'user_id': self.composio_user(user_id),
                'connected_account_id': account.id,
                'arguments': arguments,
                'version': tool.version,
            },
        )
        if result is None:
            raise ComposioError(f'{tool.name} was not found')
        if not result.get('successful', False):
            raise ComposioError(str(result.get('error') or 'the tool failed'))
        return result.get('data')


# What we read of Composio's answers; anything else in them is ignored.


class _Category(BaseModel):
    name: str = ''


class _ToolkitMeta(BaseModel):
    logo: str | None = None
    description: str | None = None
    categories: list[_Category] = []


class _Toolkit(BaseModel):
    slug: str
    name: str | None = None
    meta: _ToolkitMeta = _ToolkitMeta()


class _Tool(BaseModel):
    slug: str
    name: str | None = None
    description: str | None = None
    input_parameters: dict[str, Any] | None = None
    tags: list[str] = []
    version: str | None = None


class _Error(BaseModel):
    message: str | None = None
    slug: str | None = None


class _ErrorBody(BaseModel):
    error: _Error | str | None = None


def toolkit_from(item: dict[str, Any]) -> Toolkit:
    found = _Toolkit.model_validate(item)
    return Toolkit(
        slug=found.slug,
        name=found.name or found.slug,
        logo=found.meta.logo or '',
        description=found.meta.description or '',
        categories=tuple(c.name for c in found.meta.categories if c.name),
    )


def tool_from(item: dict[str, Any]) -> Tool:
    found = _Tool.model_validate(item)
    return Tool(
        name=found.slug,
        title=found.name or found.slug,
        description=found.description or '',
        parameters=found.input_parameters or {'type': 'object', 'properties': {}},
        read_only='readOnlyHint' in found.tags,
        version=found.version or 'latest',
    )


def error_message(response: httpx.Response) -> str:
    try:
        error = _ErrorBody.model_validate_json(response.content).error
    except ValidationError:
        return response.reason_phrase
    if isinstance(error, _Error):
        return (error.message or error.slug or response.reason_phrase)[:300]
    return (error or response.reason_phrase)[:300]
