"""Integrations below the app: user-given addresses reach public hosts only, an MCP server's secrets are sealed per
user and per server, and a service the model names is matched to the user's connection or to an app to connect."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import httpx2
import logfire
import pytest
from cryptography.exceptions import InvalidTag
from logfire.testing import CaptureLogfire
from sites.integrations import API_KEY, NOTES_TOKEN, Account, FakeComposio, NotesServer

from sammy import crypto, store
from sammy.db import Pool, create_pool, migrate
from sammy.integrations import Connection, Integrations, Offer, catalog, egress, mcp, oauth
from sammy.integrations.base import IntegrationError
from sammy.integrations.composio import Composio, Toolkit
from sammy.settings import Settings

pytestmark = pytest.mark.anyio
KEY = crypto.deployment_key(crypto.new_key())


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    await migrate(database_url)
    pool = create_pool(database_url)
    await pool.open()
    yield pool
    await pool.close()


@pytest.fixture
def composio() -> Iterator[FakeComposio]:
    fake = FakeComposio()
    fake.start()
    yield fake
    fake.stop()


@pytest.mark.parametrize(
    'url', ['http://127.0.0.1:9/', 'http://localhost:9/', 'http://169.254.169.254/latest/meta-data', 'http://10.0.0.1/']
)
async def test_user_given_addresses_reach_public_hosts_only(url: str) -> None:
    async with egress.public_client(allow_private=False) as http:
        with pytest.raises(httpx2.ConnectError, match='private network'):
            await http.get(url)


@pytest.mark.filterwarnings('ignore:Found propagated trace context')  # the fake server is in this process
async def test_a_servers_address_is_never_traced(capfire: CaptureLogfire) -> None:
    """A user's MCP server URL can hold a key; Logfire's httpx instrumentation exports every URL it sees."""
    logfire.instrument_httpx()
    server = NotesServer('token')
    server.start()
    try:
        secret = mcp.Secret(url=f'{server.mcp_url}?key=SUPERSECRET', headers={'Authorization': f'Bearer {NOTES_TOKEN}'})
        assert await mcp.call_tool(secret, 'list_notes', {}, allow_private=True) == '["Buy oat milk"]'
        async with httpx.AsyncClient() as plain:
            await plain.get(f'{server.url}/elsewhere')  # what the app calls itself is still traced
    finally:
        server.stop()
    spans = str(capfire.exporter.exported_spans_as_dict())
    assert 'SUPERSECRET' not in spans and NOTES_TOKEN not in spans
    assert '/elsewhere' in spans


def test_a_server_address_is_https_without_credentials() -> None:
    assert egress.check_url(' https://mcp.example.com/mcp ', allow_private=False) == 'https://mcp.example.com/mcp'
    for url, problem in [
        ('http://mcp.example.com/mcp', 'https://'),
        ('https:///mcp', 'no server name'),
        ('https://user:key@mcp.example.com/mcp', 'header'),
    ]:
        with pytest.raises(ValueError, match=problem):
            egress.check_url(url, allow_private=False)
    assert egress.check_url('http://127.0.0.1:8000/mcp', allow_private=True)  # tests only


async def test_a_servers_secret_is_sealed_for_its_user_and_server(pool: Pool) -> None:
    async with pool.connection() as connection, connection.transaction():
        alice = await store.create_user(connection, 'alice@example.test', 'x')
        bob = await store.create_user(connection, 'bob@example.test', 'x')
        assert alice is not None and bob is not None
        secret = mcp.Secret(url='https://notes.example.com/mcp?key=k1', headers={'Authorization': 'Bearer t1'})
        first = await mcp.add(
            connection, KEY, user_id=alice.id, name='Notes', auth='headers', status='ready', secret=secret
        )
        second = await mcp.add(
            connection, KEY, user_id=alice.id, name='Wiki', auth='headers', status='ready', secret=secret
        )
        with pytest.raises(mcp.NameTaken):
            await mcp.add(connection, KEY, user_id=alice.id, name='notes!', auth='none', status='ready', secret=secret)
        assert (first.slug, first.host) == ('notes', 'notes.example.com')
        assert await mcp.load_secret(connection, KEY, first) == secret

        # Bob cannot see, read or remove it.
        assert await mcp.list_for(connection, bob.id) == []
        assert await mcp.get(connection, bob.id, server_id=first.id) is None
        assert not await mcp.delete(connection, bob.id, first.id)

        # A sealed secret moved onto another server, or another user's server, does not open.
        cursor = await connection.execute('SELECT secret FROM sammy.mcp_servers WHERE id = %s', (first.id,))
        row = await cursor.fetchone()
        assert row is not None
        await connection.execute('UPDATE sammy.mcp_servers SET secret = %s WHERE id = %s', (row['secret'], second.id))
        with pytest.raises(IntegrationError, match='could not be read'):
            await mcp.load_secret(connection, KEY, second)
        with pytest.raises(InvalidTag):
            crypto.open_sealed(KEY, bytes(row['secret']), label=mcp.label(bob.id, first.id))


async def test_a_sign_in_flow_is_used_once(pool: Pool) -> None:
    async with pool.connection() as connection, connection.transaction():
        alice = await store.create_user(connection, 'alice@example.test', 'x')
        assert alice is not None
        server = await mcp.add(
            connection, KEY, user_id=alice.id, name='Notes', auth='oauth', status='needs_sign_in',
            secret=mcp.Secret(url='https://notes.example.com/mcp'),
        )  # fmt: skip
        await mcp.start_flow(connection, KEY, server, 'state-1', 'verifier-1')
        assert await mcp.take_flow(connection, KEY, 'state-1') == (server, 'verifier-1')
        assert await mcp.take_flow(connection, KEY, 'state-1') is None
        assert await mcp.take_flow(connection, KEY, 'made-up') is None


async def test_a_users_apps_are_theirs_only_and_reconnecting_replaces_a_broken_one(composio: FakeComposio) -> None:
    client = Composio(api_key=API_KEY, base_url=composio.url, user_prefix='sammy:')
    try:
        assert await client.accounts('alice') == []  # the other app's user's account is not hers
        composio.accounts['ca_old'] = Account(
            id='ca_old', user_id='sammy:alice', toolkit='linear', auth_config_id='ac', status='EXPIRED'
        )
        assert [(a.id, a.status) for a in await client.accounts('alice')] == [('ca_old', 'EXPIRED')]
        composio.accounts['ca_new'] = Account(
            id='ca_new', user_id='sammy:alice', toolkit='linear', auth_config_id='ac', status='ACTIVE'
        )
        assert [(a.id, a.status) for a in await client.accounts('alice')] == [('ca_new', 'ACTIVE')]
        assert not await client.disconnect('bob', 'ca_new') and 'ca_new' in composio.accounts
    finally:
        await client.aclose()


async def test_the_service_the_model_names(pool: Pool, composio: FakeComposio, database_url: str) -> None:
    settings = Settings(
        database_url=database_url,
        session_secret='s',  # pyright: ignore[reportArgumentType]
        encryption_key=crypto.new_key(),  # pyright: ignore[reportArgumentType]
        composio_api_key=API_KEY,  # pyright: ignore[reportArgumentType]
        composio_url=composio.url,
    )
    integrations = Integrations(pool, KEY, settings)
    async with pool.connection() as connection:
        user = await store.create_user(connection, 'alice@example.test', 'x')
    assert user is not None
    try:

        async def offered(service: str) -> Connection | Offer:
            return await integrations.offer(user.id, service)

        linear = Offer(provider='composio', key='linear', name='Linear', logo='https://logos.composio.dev/api/linear')
        assert await offered('Linear') == linear
        assert await offered('linear issues') == linear
        assert await offered('GitHub') == Offer(
            provider='composio', key='github', name='GitHub', logo='https://logos.composio.dev/api/github'
        )
        # A listed MCP server: added and signed in to in one click.
        assert await offered('PostHog') == Offer(
            provider='mcp',
            key='posthog',
            name='PostHog',
            logo='https://logos.composio.dev/api/posthog',
            url='https://mcp.posthog.com/mcp',
        )
        # Not an app Composio signs users in to, or nothing like one: their own MCP server.
        assert await offered('Acme CRM') == Offer(provider='mcp', key='', name='Acme CRM')
        assert await offered('Li') == Offer(provider='mcp', key='', name='Li')

        # Once connected, the connection itself.
        async with pool.connection() as connection, connection.transaction():
            await mcp.add(
                connection, KEY, user_id=user.id, name='Acme CRM', auth='none', status='ready',
                secret=mcp.Secret(url='https://crm.example.com/mcp'),
            )  # fmt: skip
        found = await offered('acme crm')
        assert isinstance(found, Connection) and (found.key, found.state) == ('mcp:acme-crm', 'connected')
    finally:
        await integrations.aclose()


def app(slug: str, name: str) -> Toolkit:
    return Toolkit(
        slug=slug, name=name, logo=f'https://logos.example/{slug}', description=f'{name} things', categories=('x',)
    )


def test_the_page_lists_the_featured_by_kind_then_every_other_app() -> None:
    apps = {
        a.slug: a
        for a in [
            app('zoom', 'Zoom'),
            app('gmail', 'Gmail'),
            app('airtable', 'Airtable'),
            app('slack', 'Slack'),
            app('linear', 'Linear'),
        ]
    }
    listed = catalog.entries(apps)
    # Featured by kind (chat, issues, email, analytics), apps only where Composio has them; then the rest by name.
    assert [(e['key'], e['kind'], e['featured']) for e in listed] == [
        ('slack', 'chat', True),
        ('linear', 'issues', True),
        ('gmail', 'email', True),
        ('posthog', 'analytics', True),
        ('airtable', None, False),
        ('zoom', None, False),
    ]
    assert listed[0] == {
        'key': 'slack',
        'slug': 'slack',
        'name': 'Slack',
        'logo': 'https://logos.example/slack',
        'description': 'Slack things',
        'categories': ['x'],
        'kind': 'chat',
        'kind_label': 'Chat',
        'featured': True,
        'provider': 'composio',
        'url': None,
        'host': None,
    }
    # Without Composio: only the MCP servers.
    [posthog] = catalog.entries({})
    assert (posthog['key'], posthog['provider'], posthog['url'], posthog['host'], posthog['kind_label']) == (
        'posthog',
        'mcp',
        'https://mcp.posthog.com/mcp',
        'mcp.posthog.com',
        'Analytics and monitoring',
    )
    assert posthog['logo'] == 'https://logos.composio.dev/api/posthog' and posthog['description']


def test_a_named_service_with_a_listed_mcp_server() -> None:
    for name in ('PostHog', 'posthog events'):
        preset = catalog.mcp_preset(name)
        assert preset is not None and (preset.key, preset.url) == ('posthog', 'https://mcp.posthog.com/mcp')
    assert catalog.mcp_preset('Linear') is None  # through Composio
    assert catalog.mcp_preset('Acme CRM') is None


def test_a_blank_composio_key_means_no_composio(database_url: str) -> None:
    settings = Settings(
        database_url=database_url,
        session_secret='s',  # pyright: ignore[reportArgumentType]
        encryption_key=crypto.new_key(),  # pyright: ignore[reportArgumentType]
        composio_api_key='  ',  # pyright: ignore[reportArgumentType]  # `COMPOSIO_API_KEY=` in a copied .env.example
    )
    assert settings.composio_api_key is None


CLIENT = oauth.OAuthClient(
    client_id='monty',
    issuer='https://auth.example.com',
    authorization_endpoint='https://auth.example.com/authorize',
    token_endpoint='https://auth.example.com/token',
    resource='https://mcp.example.com/mcp',
)


def token_server(status: int) -> httpx2.AsyncClient:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if status == 200:
            return httpx2.Response(200, json={'access_token': 'new', 'token_type': 'bearer', 'expires_in': 60})
        return httpx2.Response(status, json={'error': 'invalid_grant'})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer))


async def test_only_a_refused_refresh_means_signing_in_again() -> None:
    tokens = oauth.Tokens(access_token='old', refresh_token='r1', expires_at=0)
    async with token_server(200) as http:
        refreshed = await oauth.refresh(http, CLIENT, tokens)
        assert refreshed is not None and refreshed.access_token == 'new' and refreshed.refresh_token == 'r1'
    for refused in (400, 401):
        async with token_server(refused) as http:
            assert await oauth.refresh(http, CLIENT, tokens) is None
    # The sign-in server down or failing: the sign-in stands, and the next use tries again.
    async with token_server(503) as http:
        with pytest.raises(IntegrationError, match='failed \\(503\\)'):
            await oauth.refresh(http, CLIENT, tokens)

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError('down')

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(unreachable)) as http:
        with pytest.raises(IntegrationError, match='could not be reached'):
            await oauth.refresh(http, CLIENT, tokens)


async def test_a_registration_that_cannot_be_sent_says_so() -> None:
    metadata = {
        'issuer': 'https://auth.example.com',
        'authorization_endpoint': 'https://auth.example.com/authorize',
        'token_endpoint': 'https://auth.example.com/token',
        'registration_endpoint': 'https://auth.example.com/register',
        'code_challenge_methods_supported': ['S256'],
    }

    def server(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/.well-known/oauth-authorization-server':
            return httpx2.Response(200, json=metadata)
        if request.url.path == '/register':
            raise httpx2.ConnectError('auth.example.com is on a private network')
        return httpx2.Response(404)

    unauthorized = httpx2.Response(401, request=httpx2.Request('POST', 'https://mcp.example.com/mcp'))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as http:
        with pytest.raises(IntegrationError, match='could not register'):
            await oauth.register(http, 'https://mcp.example.com/mcp', unauthorized, 'https://monty.test/cb')
