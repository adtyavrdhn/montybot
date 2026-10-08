"""Signing in to an MCP server with OAuth, as the MCP authorization spec has it, from a web server.

```
add server  -> POST it `initialize`: 401 with WWW-Authenticate
discover    protected resource metadata (RFC 9728) -> its authorization server's metadata (RFC 8414 / OIDC)
register    dynamic client registration (RFC 7591), redirect URI <PUBLIC_URL>/integrations/mcp/callback
authorize   the user's browser goes to the authorization endpoint: PKCE S256, `state`, `resource` (RFC 8707)
callback    code + state -> tokens, kept sealed with the server (montybot.integrations.mcp)
refresh     before a call, when the access token is about to expire
```

The flow spans two requests, possibly on two servers, so nothing waits in memory: `state` and the PKCE verifier are
in Postgres between them (`montybot.mcp_oauth_flows`). The discovery and parsing helpers are the MCP SDK's; every
request goes through `egress.public_client`, as each URL here comes from the MCP server.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx2
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
)
from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from mcp_types import LATEST_PROTOCOL_VERSION
from pydantic import AnyUrl, BaseModel, ValidationError

from montybot.integrations.base import IntegrationError

EXPIRY_MARGIN = 60
"""Refresh an access token this many seconds before it expires."""


class Refused(IntegrationError):
    """The authorization server turned the grant down (400 or 401, such as `invalid_grant`), as opposed to being
    unreachable or failing: only this means the user must sign in again."""


class OAuthClient(BaseModel):
    """Our client at one MCP server's authorization server: registered once, kept sealed with the server."""

    client_id: str
    client_secret: str | None = None
    auth_method: str = 'none'
    """How the token endpoint wants the client to authenticate: `none`, `client_secret_post` or `client_secret_basic`."""
    issuer: str
    iss_required: bool = False
    authorization_endpoint: str
    token_endpoint: str
    resource: str
    scope: str | None = None


class Tokens(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None

    @property
    def expiring(self) -> bool:
        return self.expires_at is not None and self.expires_at - EXPIRY_MARGIN < time.time()


def tokens_from(token: OAuthToken, previous: Tokens | None = None) -> Tokens:
    return Tokens(
        access_token=token.access_token,
        # A server that does not rotate refresh tokens leaves the old one in force.
        refresh_token=token.refresh_token or (previous.refresh_token if previous else None),
        expires_at=time.time() + token.expires_in if token.expires_in else None,
    )


async def needs_sign_in(http: httpx2.AsyncClient, url: str) -> httpx2.Response | None:
    """The server's 401 or 403 if it wants a sign-in before anything; None if it answers without one."""
    response = await http.post(
        url,
        json={
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': LATEST_PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'Monty', 'version': '1'},
            },
        },
        headers={'Accept': 'application/json, text/event-stream'},
    )
    await response.aclose()
    return response if response.status_code in (401, 403) else None


async def register(http: httpx2.AsyncClient, url: str, unauthorized: httpx2.Response, redirect_uri: str) -> OAuthClient:
    """Discover the server's authorization server and register Monty with it. Raises `IntegrationError` with words
    for the user if the server cannot be signed in to this way."""
    resource_metadata = await _protected_resource(http, url, unauthorized)
    auth_server = str(resource_metadata.authorization_servers[0]) if resource_metadata else None
    metadata = await _authorization_server(http, auth_server, url)
    if metadata is None:
        raise IntegrationError('This server asks for a sign-in, but does not say where. Add it with a token instead.')
    if 'S256' not in (metadata.code_challenge_methods_supported or []):
        raise IntegrationError("This server's sign-in does not support PKCE, which Monty needs to sign in safely.")
    if metadata.registration_endpoint is None:
        raise IntegrationError(
            "This server's sign-in does not let Monty register itself. Add it with a token instead, if it has one."
        )
    scope = extract_scope_from_www_auth(unauthorized) or (
        ' '.join(resource_metadata.scopes_supported)
        if resource_metadata and resource_metadata.scopes_supported
        else None
    )
    auth_method = _auth_method(metadata)
    client_metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method=auth_method,  # pyright: ignore[reportArgumentType]  # one of the spec's names
        grant_types=['authorization_code', 'refresh_token'],
        response_types=['code'],
        client_name='Monty',
        scope=scope,
        application_type='web',
    )
    request = create_client_registration_request(metadata, client_metadata, str(metadata.issuer))
    try:
        info = await handle_registration_response(await http.send(request))
    except (OAuthFlowError, httpx2.HTTPError) as error:  # refused, unreachable, or a private address
        raise IntegrationError('Monty could not register with this server for a sign-in.') from error
    return OAuthClient(
        client_id=info.client_id,
        client_secret=info.client_secret,
        auth_method=info.token_endpoint_auth_method or auth_method,
        issuer=str(metadata.issuer),
        iss_required=bool(metadata.authorization_response_iss_parameter_supported),
        authorization_endpoint=str(metadata.authorization_endpoint),
        token_endpoint=str(metadata.token_endpoint),
        resource=str(resource_metadata.resource) if resource_metadata else url,
        scope=scope,
    )


def _auth_method(metadata: OAuthMetadata) -> str:
    supported = metadata.token_endpoint_auth_methods_supported
    if supported is None or 'none' in supported:
        return 'none'
    return 'client_secret_post' if 'client_secret_post' in supported else 'client_secret_basic'


async def _protected_resource(
    http: httpx2.AsyncClient, url: str, unauthorized: httpx2.Response
) -> ProtectedResourceMetadata | None:
    hinted = extract_resource_metadata_from_www_auth(unauthorized)
    for candidate in build_protected_resource_metadata_discovery_urls(hinted, url):
        try:
            found = await handle_protected_resource_response(await http.send(create_oauth_metadata_request(candidate)))
        except httpx2.HTTPError:
            continue
        if found is not None:
            return found
    return None


async def _authorization_server(http: httpx2.AsyncClient, auth_server: str | None, url: str) -> OAuthMetadata | None:
    for candidate in build_oauth_authorization_server_metadata_discovery_urls(auth_server, url):
        try:
            keep_trying, found = await handle_auth_metadata_response(
                await http.send(create_oauth_metadata_request(candidate))
            )
        except httpx2.HTTPError:
            continue
        if found is not None:
            return found
        if not keep_trying:
            return None
    return None


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def authorize_url(client: OAuthClient, *, state: str, verifier: str, redirect_uri: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    query = {
        'response_type': 'code',
        'client_id': client.client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': client.resource,
        **({'scope': client.scope} if client.scope else {}),
    }
    separator = '&' if '?' in client.authorization_endpoint else '?'
    return f'{client.authorization_endpoint}{separator}{urlencode(query)}'


def check_issuer(client: OAuthClient, iss: str | None) -> None:
    """RFC 9207: the code came from the authorization server we sent the user to."""
    if iss is not None and iss != client.issuer:
        raise IntegrationError('The sign-in came back from a different server than it went to.')
    if iss is None and client.iss_required:
        raise IntegrationError('The sign-in came back without saying which server it came from.')


async def exchange(
    http: httpx2.AsyncClient, client: OAuthClient, *, code: str, verifier: str, redirect_uri: str
) -> Tokens:
    return tokens_from(
        await _token_request(
            http,
            client,
            {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri, 'code_verifier': verifier},
        )
    )


async def refresh(http: httpx2.AsyncClient, client: OAuthClient, tokens: Tokens) -> Tokens | None:
    """New tokens, or None if the user must sign in again (no refresh token, or the server refused it). A server
    that is down or failing raises `IntegrationError`: the sign-in stands, and the next use tries again."""
    if tokens.refresh_token is None:
        return None
    try:
        token = await _token_request(
            http, client, {'grant_type': 'refresh_token', 'refresh_token': tokens.refresh_token}
        )
    except Refused:
        return None
    return tokens_from(token, tokens)


async def _token_request(http: httpx2.AsyncClient, client: OAuthClient, form: dict[str, str]) -> OAuthToken:
    form = {**form, 'resource': client.resource}
    auth: Any = None
    if client.auth_method == 'client_secret_basic' and client.client_secret:
        auth = httpx2.BasicAuth(client.client_id, client.client_secret)
    else:
        form['client_id'] = client.client_id
        if client.auth_method == 'client_secret_post' and client.client_secret:
            form['client_secret'] = client.client_secret
    try:
        response = await http.post(client.token_endpoint, data=form, auth=auth, headers={'Accept': 'application/json'})
    except httpx2.HTTPError as error:
        raise IntegrationError('The sign-in server could not be reached.') from error
    if response.status_code in (400, 401):
        raise Refused('The sign-in server refused the sign-in.')
    if response.status_code != 200:
        raise IntegrationError(f'The sign-in server failed ({response.status_code}). Please try again in a moment.')
    try:
        return OAuthToken.model_validate_json(response.content)
    except ValidationError as error:
        raise IntegrationError('The sign-in server answered with something Monty could not read.') from error
