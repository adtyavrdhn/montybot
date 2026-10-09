"""Integrations: apps through Composio and the user's own MCP servers.

A user mentions Linear, the chat offers to connect it, they sign in on Composio's page, and the run carries on and
uses it; a change in an app waits for approval; each user sees and uses only their own connections, in a Composio
project another app shares; an MCP server works with a token or an OAuth sign-in, and its secrets are sealed.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from conftest import App, Client
from sites.integrations import API_KEY, NOTES_TOKEN, OTHER_APP_USER, FakeComposio, NotesServer

pytestmark = pytest.mark.scripted


@pytest.fixture
def composio() -> Iterator[FakeComposio]:
    fake = FakeComposio()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def app_env(composio: FakeComposio) -> dict[str, str]:
    return {'COMPOSIO_API_KEY': API_KEY, 'COMPOSIO_URL': composio.url}


@pytest.fixture
def notes() -> Iterator[NotesServer]:
    server = NotesServer('token')
    server.start()
    yield server
    server.stop()


@pytest.fixture
def oauth_notes() -> Iterator[NotesServer]:
    server = NotesServer('oauth')
    server.start()
    yield server
    server.stop()


def user_id(client: Client) -> str:
    return client.http.get('/api/me').json()['id']


def in_a_browser(url: str) -> httpx.Response:
    """The user's own browser: none of the web app's cookies (on a Mac it is not the app), following redirects."""
    with httpx.Client(follow_redirects=True, timeout=30) as browser:
        return browser.get(url)


def connect_app(client: Client, slug: str) -> httpx.Response:
    """Connect an app as the user would from the web app: the link, then signing in on Composio's page, which sends
    the browser back to Sammy."""
    response = client.http.post(f'/api/integrations/apps/{slug}/connect', json={})
    assert response.status_code == 200, response.text
    return in_a_browser(response.json()['url'])


def connections(client: Client) -> list[dict[str, str]]:
    response = client.http.get('/api/integrations')
    assert response.status_code == 200, response.text
    return response.json()['connections']


def test_mentioning_linear_offers_its_own_server_and_an_app_connected_instead_is_used(
    client: Client, composio: FakeComposio
) -> None:
    client.sign_up()
    me = user_id(client)
    thread = client.ask("yo what's on my linear")

    # Linear's own MCP server (pydantic-ai-harness integrates it) is what the card offers, in one click.
    ask = client.wait_for_ask(thread, 'connect')
    assert ask['prompt'] == 'Connect Linear so I can look up your issues.'
    assert ask['integration'] == {
        'provider': 'mcp',
        'key': 'linear',
        'name': 'Linear',
        'logo': 'https://logos.composio.dev/api/linear',
        'url': 'https://mcp.linear.app/mcp',
        'auth': 'oauth',
    }
    listed = client.http.get('/api/threads').json()
    assert [(t['id'], t['waiting_for']) for t in listed] == [(thread, 'connect')]

    # The user connects Linear through Composio instead (under More apps), and says so on the card.
    page = connect_app(client, 'linear')
    assert page.status_code == 200 and 'Linear is connected' in page.text
    client.answer(ask, connected=True)

    reply = client.wait_for_reply(thread)
    assert 'Fix the login page' in reply
    assert 'secret' not in reply  # the other app's user's issues
    assert {'role': 'event', 'text': 'You connected Linear'} in client.thread(thread)['messages']

    # It ran as this user, on this user's own account, at the version it listed.
    [ran] = composio.executed
    assert ran['user_id'] == f'sammy:{me}'
    assert composio.accounts[ran['connected_account_id']].user_id == f'sammy:{me}'
    assert ran['version'] == '20260924_00'
    # Sammy made its own auth config; the other app's was left alone.
    assert [c['name'] for c in composio.auth_configs] == ['viktor-linear', 'sammy-linear']

    assert [(c['key'], c['provider'], c['state']) for c in connections(client)] == [('linear', 'composio', 'connected')]
    apps = client.http.get('/api/integrations/apps').json()
    # The featured ones by kind, the MCP servers pydantic-ai-harness integrates first in theirs, and apps only where
    # Composio has them; then the other apps, Composio's own GitHub and Linear among them.
    assert [(a['key'], a['provider'], a['featured']) for a in apps] == [
        ('github', 'mcp', True),
        ('linear', 'mcp', True),
        ('notion', 'mcp', True),
        ('gmail', 'composio', True),
        ('grain', 'mcp', True),
        ('day_ai', 'mcp', True),
        ('posthog', 'mcp', True),
        ('logfire', 'mcp', True),
        ('logfire_eu', 'mcp', True),
        ('pylon', 'mcp', True),
        ('github', 'composio', False),
        ('linear', 'composio', False),
    ]
    assert [a['name'] for a in apps if not a['featured']] == ['GitHub via Composio', 'Linear via Composio']


def test_mentioning_gmail_offers_its_app_and_the_run_carries_on_once_connected(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Check my Gmail')

    ask = client.wait_for_ask(thread, 'connect')
    assert ask['integration'] == {'provider': 'composio', 'key': 'gmail', 'name': 'Gmail', 'logo': ''}
    page = connect_app(client, 'gmail')
    assert page.status_code == 200 and 'Gmail is connected' in page.text

    # Composio's page coming back is the answer: the run carries on by itself.
    assert 'Gmail is connected now, as `gmail`' in client.wait_for_reply(thread)
    assert {'role': 'event', 'text': 'You connected Gmail'} in client.thread(thread)['messages']


def test_a_change_in_an_app_waits_for_approval(client: Client, composio: FakeComposio) -> None:
    client.sign_up()
    connect_app(client, 'linear')
    thread = client.ask('Create a Linear issue called Ship integrations')

    ask = client.wait_for_ask(thread, 'approval')
    assert ask['prompt'] == 'Use linear: LINEAR_CREATE_LINEAR_ISSUE {"title": "Ship integrations"}'
    assert composio.executed == []  # nothing done before the user says yes

    client.answer(ask, approved=True)
    assert 'Ship integrations' in client.wait_for_reply(thread)
    [account] = [a for a in composio.accounts.values() if a.user_id == f'sammy:{user_id(client)}']
    assert composio.issues[account.id] == ['Fix the login page', 'Ship integrations']


def test_an_action_the_user_always_allows_runs_end_to_end_without_stopping(
    client: Client, composio: FakeComposio
) -> None:
    client.sign_up()
    connect_app(client, 'linear')
    first = client.ask('Create a Linear issue called Weekly groceries')
    ask = client.wait_for_ask(first, 'approval')
    rule = {
        'tool': 'call_integration_tool',
        'scope': 'linear',
        'name': 'LINEAR_CREATE_LINEAR_ISSUE',
        'risk': None,
        'summary': 'Use linear: LINEAR_CREATE_LINEAR_ISSUE',
    }
    assert ask['rule'] == rule  # the card offers "Always allow this"
    client.answer(ask, approved=True, remember=True)
    assert 'Weekly groceries' in client.wait_for_reply(first)
    [kept] = client.http.get('/api/approvals').json()['rules']
    assert kept == {'id': kept['id'], **rule, 'allow_risky': False}

    # The same change again, in another chat: nobody is asked, and the chat says why.
    second = client.ask('Create a Linear issue called Weekly cleaning')
    assert 'Weekly cleaning' in client.wait_for_reply(second)
    assert client.thread(second)['messages'][1] == {
        'role': 'event',
        'text': 'Approved by your rule: Use linear: LINEAR_CREATE_LINEAR_ISSUE {"title": "Weekly cleaning"}',
    }
    [account] = [a for a in composio.accounts.values() if a.user_id == f'sammy:{user_id(client)}']
    assert composio.issues[account.id][-2:] == ['Weekly groceries', 'Weekly cleaning']

    # Removed in settings: the next one asks again.
    assert client.http.delete(f'/api/approvals/rules/{kept["id"]}').status_code == 200
    assert client.http.get('/api/approvals').json() == {'rules': [], 'reviewer': {'available': False, 'enabled': False}}
    third = client.ask('Create a Linear issue called Weekly laundry')
    client.answer(client.wait_for_ask(third, 'approval'), approved=False, reason='not this week')
    client.wait_for_reply(third)
    assert composio.issues[account.id][-1] == 'Weekly cleaning'


def test_not_now(client: Client, composio: FakeComposio) -> None:
    client.sign_up()
    thread = client.ask("yo what's on my linear")
    client.answer(client.wait_for_ask(thread, 'connect'), connected=False)

    assert 'chose not to connect Linear' in client.wait_for_reply(thread)
    assert {'role': 'event', 'text': 'You chose not to connect Linear'} in client.thread(thread)['messages']
    assert composio.executed == []


def test_users_see_and_use_only_their_own_connections(app: App, client: Client, composio: FakeComposio) -> None:
    client.sign_up()
    connect_app(client, 'linear')
    [alices] = connections(client)

    bob = Client(app)
    try:
        bob.sign_up()
        bobs_id = user_id(bob)
        # Not Alice's, and not the other app's user's, though the project holds both.
        assert connections(bob) == []
        assert bob.http.delete(f'/api/integrations/apps/accounts/{alices["id"]}').status_code == 404
        assert bob.http.delete('/api/integrations/apps/accounts/ca_other').status_code == 404
        assert client.http.delete('/api/integrations/apps/accounts/ca_other').status_code == 404
        assert alices['id'] in composio.accounts and 'ca_other' in composio.accounts

        # Bob's chat asks him to connect his own Gmail; Alice's does not count.
        thread = bob.ask('Check my Gmail')
        bobs_ask = bob.wait_for_ask(thread, 'connect')

        # A sign-in coming back for Alice wakes nothing of Bob's, and a made-up one is refused.
        assert connect_app(client, 'gmail').status_code == 200
        assert in_a_browser(f'{app.url}/integrations/composio/callback?state=made-up').status_code == 400
        assert bob.thread(thread)['run']['ask'] == bobs_ask
        connect_app(bob, 'gmail')
        assert 'Gmail is connected now' in bob.wait_for_reply(thread)

        # Bob's own Linear, connected through Composio, is the one his chat uses; Alice's does not count.
        connect_app(bob, 'linear')
        assert 'Fix the login page' in bob.wait_for_reply(bob.ask("yo what's on my linear"))
        assert composio.executed[-1]['user_id'] == f'sammy:{bobs_id}'
        assert composio.accounts[composio.executed[-1]['connected_account_id']].user_id == f'sammy:{bobs_id}'
    finally:
        bob.http.close()

    # Alice removes hers: gone from Composio, Bob's stays.
    for connection in connections(client):
        assert client.http.delete(f'/api/integrations/apps/accounts/{connection["id"]}').status_code == 200
    assert connections(client) == []
    assert [a.user_id for a in composio.accounts.values()] == [OTHER_APP_USER, *[f'sammy:{bobs_id}'] * 2]


def test_an_mcp_server_with_a_token(app: App, client: Client, notes: NotesServer, database_url: str) -> None:
    client.sign_up()
    add = {'name': 'My notes', 'url': notes.mcp_url}
    refused = client.http.post('/api/integrations/servers', json={**add, 'headers': {'Authorization': 'Bearer nope'}})
    assert refused.status_code == 400 and refused.json()['detail'] == 'The server refused those credentials.'
    not_mcp = client.http.post('/api/integrations/servers', json={**add, 'url': f'{app.url}/healthz'})
    assert not_mcp.status_code == 400

    token = {'Authorization': f'Bearer {NOTES_TOKEN}'}
    added = client.http.post('/api/integrations/servers', json={**add, 'headers': token})
    assert added.status_code == 201, added.text
    assert added.json()['sign_in_url'] is None
    assert client.http.post('/api/integrations/servers', json={**add, 'headers': token}).status_code == 409
    [server] = connections(client)
    assert (server['key'], server['provider'], server['state'], server['detail']) == (
        'mcp:my-notes',
        'mcp',
        'connected',
        '127.0.0.1',
    )

    thread = client.ask('What notes are in mcp:my-notes')
    assert 'Buy oat milk' in client.wait_for_reply(thread)
    assert notes.calls == ['list_notes']  # read only: no approval

    # The address and the token are sealed with the user's key.
    with psycopg.connect(database_url) as connection:
        [(secret,)] = connection.execute('SELECT secret FROM sammy.mcp_servers').fetchall()
    assert NOTES_TOKEN.encode() not in bytes(secret) and notes.mcp_url.encode() not in bytes(secret)

    bob = Client(app)
    try:
        bob.sign_up()
        assert connections(bob) == []
        assert bob.http.delete(f'/api/integrations/servers/{server["id"]}').status_code == 404
        # Bob's agent naming Alice's server reaches nothing.
        reply = bob.wait_for_reply(bob.ask('What notes are in mcp:my-notes'))
        assert 'mcp:my-notes is not connected' in reply and 'oat milk' not in reply
        assert notes.calls == ['list_notes']
    finally:
        bob.http.close()
    assert client.http.delete(f'/api/integrations/servers/{server["id"]}').status_code == 200
    assert connections(client) == []


def test_an_mcp_server_with_an_oauth_sign_in(app: App, client: Client, oauth_notes: NotesServer) -> None:
    oauth_notes.consent.token_seconds = 30  # inside Sammy's margin: each use refreshes the token first
    client.sign_up()
    thread = client.ask('Search my Acme Wiki')
    ask = client.wait_for_ask(thread, 'connect')
    assert ask['integration'] == {'provider': 'mcp', 'key': '', 'name': 'Acme Wiki', 'logo': ''}

    added = client.http.post('/api/integrations/servers', json={'name': 'Acme Wiki', 'url': oauth_notes.mcp_url})
    assert added.status_code == 201, added.text
    sign_in_url = added.json()['sign_in_url']
    assert sign_in_url.startswith(oauth_notes.url)
    assert [c['state'] for c in connections(client)] == ['needs_sign_in']
    assert client.thread(thread)['run']['ask'] == ask  # not signed in yet: still waiting

    # The user signs in on the server's page in their own browser, which comes back to Sammy.
    with httpx.Client(timeout=30) as browser:
        to_sammy = browser.get(sign_in_url).headers['location']
        assert to_sammy.startswith(f'{app.url}/integrations/mcp/callback?')
        page = browser.get(to_sammy)
        assert page.status_code == 200 and 'Acme Wiki is connected' in page.text
        assert browser.get(to_sammy).status_code == 400  # a sign-in is used once
    assert [c['state'] for c in connections(client)] == ['connected']

    # The run looks again for itself, and finds the server by the name the user gave it.
    assert client.wait_for_reply(thread).startswith('Acme Wiki is connected now, as `mcp:acme-wiki`.')
    assert 'Buy oat milk' in client.wait_for_reply(client.ask('What notes are in mcp:acme-wiki', thread))
    assert oauth_notes.calls == ['list_notes']
    # The sign-in's refresh token was used up for new ones: the tokens were refreshed before use.
    assert len(oauth_notes.consent.refresh) == 1 and len(oauth_notes.consent.access) > 1
