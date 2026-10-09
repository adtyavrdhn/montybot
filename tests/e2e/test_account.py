"""#135: a user takes everything of theirs with them, or deletes their account and leaves no row of it behind."""

from __future__ import annotations

import base64
import binascii
import email
import io
import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from conftest import App, Client
from dbos import DBOSClient
from helpers import eventually
from psycopg import sql
from sites.integrations import API_KEY, NOTES_TOKEN, FakeComposio, NotesServer
from sites.shop import Shop
from test_notifications import Mailbox, mailbox, subscription_keys  # noqa: F401  # the fixture, and the SMTP server

pytestmark = pytest.mark.scripted
PASSWORD = 'correct horse'
PUSH_ENDPOINT = 'https://fcm.googleapis.com/fcm/send/adas-phone'


@pytest.fixture
def composio() -> Iterator[FakeComposio]:
    fake = FakeComposio()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def notes() -> Iterator[NotesServer]:
    server = NotesServer('token')
    server.start()
    yield server
    server.stop()


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def app_env(composio: FakeComposio, mailbox: Mailbox) -> dict[str, str]:  # noqa: F811
    return {
        'COMPOSIO_API_KEY': API_KEY,
        'COMPOSIO_URL': composio.url,
        'SMTP_URL': f'smtp://127.0.0.1:{mailbox.server_address[1]}',
        'EXPORT_INLINE_BYTES': '1',  # any account with something in it is exported in the background
    }


def mentions(database_url: str, user_id: str) -> list[str]:
    """Every table, ours and DBOS's, with a row that names the user: in plain text, or inside a value DBOS pickled
    and stored as base64."""
    found: set[str] = set()
    with psycopg.connect(database_url) as connection:
        tables = connection.execute(
            'SELECT table_schema, table_name FROM information_schema.tables '
            "WHERE table_schema IN ('sammy', 'dbos') AND table_type = 'BASE TABLE'"
        ).fetchall()
        assert len(tables) > 20
        for schema, table in tables:
            query = sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t').format(
                sql.Identifier(schema), sql.Identifier(table)
            )
            for (row,) in connection.execute(query):
                if user_id in row or any(user_id.encode() in decoded(value) for value in strings(json.loads(row))):
                    found.add(f'{schema}.{table}')
    return sorted(found)


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():  # pyright: ignore[reportUnknownVariableType]
            yield from strings(item)  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, list):
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            yield from strings(item)  # pyright: ignore[reportUnknownArgumentType]


def decoded(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return b''


def unzipped(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_deleting_an_account_leaves_no_row_of_it(
    app: App,
    client: Client,
    composio: FakeComposio,
    notes: NotesServer,
    shop: Shop,
    mailbox: Mailbox,  # noqa: F811
    database_url: str,
    workspaces_dir: Path,
) -> None:
    ada = client.sign_up('ada@example.test', PASSWORD)['id']
    bob = Client(app)
    bob.sign_up()
    bobs_chat = bob.ask('Say hello')
    bob.wait_for_reply(bobs_chat)

    # Ada has a chat with a file, a memory, a schedule that has run, and a task waiting for her answer.
    upload = client.http.post(
        '/api/attachments', content=b'eggs, milk', headers={'Content-Type': 'text/plain', 'X-Filename': 'list.txt'}
    )
    chat = client.http.post('/api/threads', json={'text': 'Say hello', 'attachments': [upload.json()['id']]})
    client.wait_for_reply(chat.json()['thread_id'])
    colour = client.ask('Ask me my favourite colour')
    client.answer(client.wait_for_ask(colour, 'question'), text='green')
    client.wait_for_reply(colour)
    weekly = client.ask(f'Every Monday at 9, fill my cart at {shop.url} with eggs and milk')
    client.answer(client.wait_for_ask(weekly, 'approval'), approved=True)
    assert 'Scheduled' in client.wait_for_reply(weekly)
    [schedule] = client.http.get('/api/schedules').json()
    dbos = DBOSClient(system_database_url=database_url)
    try:
        dbos.trigger_schedule(f'sammy-schedule-{schedule["id"]}')
    finally:
        dbos.destroy()
    eventually(lambda: client.thread(schedule['thread_id'])['run'], what='the schedule to run')
    waiting = client.ask('Ask me my favourite colour')
    client.wait_for_ask(waiting, 'question')

    # An app, an MCP server with a token, a phone for notifications, and a file of her own.
    link = client.http.post('/api/integrations/apps/linear/connect', json={}).json()['url']
    assert httpx.get(link, follow_redirects=True, timeout=30).status_code == 200
    server = {'name': 'Notes', 'url': notes.mcp_url, 'headers': {'Authorization': f'Bearer {NOTES_TOKEN}'}}
    assert client.http.post('/api/integrations/servers', json=server).status_code == 201
    push = {'endpoint': PUSH_ENDPOINT, 'keys': subscription_keys()}
    assert client.http.post('/api/push/subscriptions', json=push).status_code == 201
    (workspaces_dir / ada).mkdir(parents=True, exist_ok=True)
    (workspaces_dir / ada / 'notes.txt').write_text('call the vet')

    # Her export is built in the background and emailed as a link, which works without signing in.
    started = client.http.get('/api/export')
    assert started.status_code == 202 and started.json() == {'emailed': True, 'email': 'ada@example.test'}
    message = eventually(
        lambda: next((m for m in mailbox.messages if 'Your Sammy data is ready' in m), None), what='the email'
    )
    body = email.message_from_string(message).get_payload(decode=True)
    assert isinstance(body, bytes)
    [download] = re.findall(r'http://\S+/api/exports/\S+', body.decode())
    response = httpx.get(download, timeout=30)
    assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
    entries = unzipped(response.content)
    assert {'account.json', 'memories.json', 'schedules.json', 'integrations.json', 'files/notes.txt'} <= set(entries)
    assert sum(name.endswith('/chat.md') for name in entries) == 4  # hello, two colours, and the schedule's chat
    assert json.loads(entries['memories.json'])[0]['text'] == 'Favourite colour: green'
    assert {(i['name'], i['kind']) for i in json.loads(entries['integrations.json'])} == {
        ('Linear', 'composio'),
        ('Notes', 'mcp'),
    }
    everything = b'\n'.join(entries.values())
    for secret in (NOTES_TOKEN, notes.mcp_url, PUSH_ENDPOINT):
        assert secret.encode() not in everything

    # Deleting takes her password.
    wrong = client.http.request('DELETE', '/api/account', json={'password': 'wrong horse'})
    assert wrong.status_code == 403
    before = mentions(database_url, ada)
    expected = ['sammy.users', 'sammy.threads', 'sammy.runs', 'sammy.memories', 'sammy.attachments']
    assert set(expected + ['sammy.mcp_servers', 'sammy.push_subscriptions']) <= set(before)
    assert any(table.startswith('dbos.') for table in before), before  # the scan sees into DBOS's pickles

    deleted = client.http.request('DELETE', '/api/account', json={'password': PASSWORD})
    assert deleted.status_code == 200, deleted.text
    assert client.http.get('/api/me').status_code == 401
    assert client.http.post('/api/signin', json={'email': 'ada@example.test', 'password': PASSWORD}).status_code == 401

    assert mentions(database_url, ada) == []
    assert not (workspaces_dir / ada).exists()
    assert composio.accounts_of(f'sammy:{ada}') == []
    assert httpx.get(download, timeout=30).status_code == 404
    with psycopg.connect(database_url) as connection:
        assert connection.execute('SELECT count(*) FROM dbos.workflow_schedules').fetchone() == (0,)
    # Bob's account is as it was.
    assert bob.thread(bobs_chat)['messages'][-1]['text'] == 'Hello! I am Sammy.'


def test_a_new_account_is_exported_at_once(client: Client) -> None:
    client.sign_up('cy@example.test')
    response = client.http.get('/api/export')
    assert response.status_code == 200
    assert re.fullmatch(
        r'attachment; filename="sammy-export-\d{4}-\d{2}-\d{2}\.zip"', response.headers['content-disposition']
    )
    entries = unzipped(response.content)
    assert sorted(entries) == ['account.json', 'integrations.json', 'memories.json', 'schedules.json']
    assert json.loads(entries['account.json'])['email'] == 'cy@example.test'
    assert json.loads(entries['memories.json']) == []
