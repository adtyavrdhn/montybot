"""Secrets the agent can use but never see (#130, `sammy/vault.py`).

The agent asks for a token with `request_secret`; the user types it into a private field; the agent's code names it
(`{{secret:notes_token}}`) in an HTTP request, and the host puts the value in on the way out, for the secret's own host
only. The value never reaches the history, a model request, an exported span, or anything the database keeps in the
clear (DBOS's recorded steps and messages included). Users list and forget their secrets in settings, and forgetting
one through the agent asks first.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from conftest import App, Client
from helpers import eventually
from psycopg import sql
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models.function import AgentInfo
from test_traces import dump_spans, serve_traced

pytestmark = pytest.mark.scripted

TOKEN = 'ntk-7Hq2Lx9Vb4Rw8Zp1'
"""The user's token for the notes API: it must never be seen anywhere but by that API."""


class NotesAPI:
    """An API that saves a note for a bearer token, and echoes back what it was sent, the token included: so the
    response the agent gets must be scrubbed."""

    def __init__(self) -> None:
        self.received: list[dict[str, str]] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get('content-length', 0))).decode()
                auth = self.headers.get('authorization', '')
                received.append({'authorization': auth, 'body': body})
                ok = auth == f'Bearer {TOKEN}'
                payload = json.dumps({'saved': ok, 'you_sent': auth, 'note': json.loads(body or '{}').get('text')})
                self.send_response(201 if ok else 401)
                self.send_header('content-type', 'application/json')
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()


@pytest.fixture
def notes_api() -> Iterator[NotesAPI]:
    api = NotesAPI()
    api.start()
    yield api
    api.stop()


def stored_where(database_url: str, text: str) -> set[str]:
    """Every table of Sammy's and DBOS's that holds `text` in the clear: as text, as bytes, or base64-encoded (DBOS
    pickles what it records, in base64)."""
    needle = text.encode()
    found: set[str] = set()
    with psycopg.connect(database_url) as connection:
        tables = connection.execute(
            'SELECT table_schema, table_name FROM information_schema.tables '
            "WHERE table_schema IN ('sammy', 'dbos') AND table_type = 'BASE TABLE'"
        ).fetchall()
        for schema, table in tables:
            query = sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(table))
            for row in connection.execute(query):
                if any(needle in form for value in row for form in forms(value)):
                    found.add(f'{schema}.{table}')
    return found


def forms(value: object) -> list[bytes]:
    if isinstance(value, bytes | memoryview):
        return [bytes(value)]
    text = value if isinstance(value, str) else repr(value)
    decoded = [text.encode()]
    with contextlib.suppress(ValueError):
        decoded.append(base64.b64decode(text, validate=True))
    return decoded


def test_a_requested_secret_is_used_in_a_request_and_never_seen(
    database_url: str, workspaces_dir: Path, notes_api: NotesAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every model request, as the model got it.
    from e2e.scripts import model

    requests: list[str] = []
    respond = model.function
    assert respond is not None

    def recorded(messages: list[ModelMessage], info: AgentInfo) -> object:
        requests.append(ModelMessagesTypeAdapter.dump_json(messages).decode())
        return respond(messages, info)

    monkeypatch.setattr(model, 'function', recorded)

    # Content in traces on: the most there is to leak into.
    with serve_traced(database_url, workspaces_dir, logfire_include_content=True) as (app, exporter):
        client = Client(app)  # pyright: ignore[reportArgumentType]
        client.sign_up()
        thread = client.ask(f'Post my note to {notes_api.url}')

        ask = client.wait_for_ask(thread, 'secret')
        assert ask['prompt'] == 'Paste your Notes API token so I can post the note.'
        assert ask['secret'] == {'name': 'notes_token', 'host': '127.0.0.1'}
        assert client.http.post(f'/api/asks/{ask["id"]}', json={'secret': 'abc'}).status_code == 422  # too short
        client.answer(ask, secret=TOKEN)

        # The API got the token; the agent got its answer, with the echoed token put back as the placeholder.
        reply = client.wait_for_reply(thread)
        assert reply.startswith('201 ')
        assert '"you_sent": "Bearer {{secret:notes_token}}"' in reply and '"note": "Buy oat milk"' in reply
        [sent] = notes_api.received
        assert sent['authorization'] == f'Bearer {TOKEN}'
        assert json.loads(sent['body']) == {'text': 'Buy oat milk'}

        # The same placeholder, to another host: refused before anything is sent.
        other = f'http://localhost:{notes_api.port}'
        refused = client.wait_for_reply(client.ask(f'Send my notes token to {other}', thread))
        assert "The secret 'notes_token' may only be sent to 127.0.0.1" in refused
        assert len(notes_api.received) == 1

        assert {'role': 'event', 'text': 'You saved the secret notes_token'} in client.thread(thread)['messages']
        assert client.http.get('/api/secrets').json() == [{'name': 'notes_token', 'host': '127.0.0.1'}]
        history = json.dumps(client.thread(thread))

        eventually(
            lambda: sum(span.name == 'run.lifecycle' for span in exporter.get_finished_spans()) >= 2 or None,
            what='both runs to finish cleanup',
        )
        spans = exporter.get_finished_spans()

    assert TOKEN not in history
    assert requests and all(TOKEN not in request for request in requests)
    assert any('{{secret:notes_token}}' in request for request in requests)

    dumped = dump_spans(spans)
    assert TOKEN not in dumped
    assert 'Buy oat milk' in dumped  # content is exported: the token is not, for that reason alone
    http_spans = [span for span in spans if span.name == 'code.http']
    assert [(span.attributes or {}).get('http.host') for span in http_spans] == ['127.0.0.1']

    # Sealed in its own row; in the clear nowhere, DBOS's recorded steps and messages included.
    assert stored_where(database_url, TOKEN) == set()
    assert 'dbos.operation_outputs' in stored_where(database_url, 'Buy oat milk')  # the search finds what is there
    with psycopg.connect(database_url) as connection:
        [(name, host)] = connection.execute('SELECT name, host FROM sammy.secrets').fetchall()
    assert (name, host) == ('notes_token', '127.0.0.1')


def test_secrets_are_each_users_own_and_forgotten_in_settings_or_after_asking(
    app: App, client: Client, notes_api: NotesAPI
) -> None:
    client.sign_up()
    note = f'Post my note to {notes_api.url}'
    thread = client.ask(note)
    client.answer(client.wait_for_ask(thread, 'secret'), secret=TOKEN)
    assert client.wait_for_reply(thread).startswith('201 ')

    bob = Client(app)
    try:
        bob.sign_up()
        assert bob.http.get('/api/secrets').json() == []
        assert bob.http.delete('/api/secrets/notes_token').status_code == 404
        # Bob's agent naming Alice's secret opens nothing.
        reply = bob.wait_for_reply(bob.ask(f'Send my notes token to {notes_api.url}'))
        assert 'No secret named notes_token. Saved secrets: none.' in reply
        assert len(notes_api.received) == 1
    finally:
        bob.http.close()

    # Asked for again in a new chat, it is there already: no card.
    assert client.wait_for_reply(client.ask(note)).startswith('201 ')
    assert len(notes_api.received) == 2

    # Forgotten in settings.
    assert client.http.delete('/api/secrets/notes_token').status_code == 200
    assert client.http.get('/api/secrets').json() == []

    # Asked for again: the user can say not now.
    declined = client.ask(note)
    client.answer(client.wait_for_ask(declined, 'secret'), saved=False)
    assert 'The user chose not to give `notes_token`' in client.wait_for_reply(declined)
    assert {'role': 'event', 'text': 'You chose not to give notes_token'} in client.thread(declined)['messages']

    # Given again, then forgotten through the agent, which asks first.
    thread = client.ask(note)
    client.answer(client.wait_for_ask(thread, 'secret'), secret=TOKEN)
    assert client.wait_for_reply(thread).startswith('201 ')
    forget = client.ask('Forget my notes token')
    ask = client.wait_for_ask(forget, 'approval')
    assert ask['prompt'] == 'Forget your secret "notes_token"'
    client.answer(ask, approved=False)
    assert 'The user said no' in client.wait_for_reply(forget)
    assert [s['name'] for s in client.http.get('/api/secrets').json()] == ['notes_token']

    forget = client.ask('Forget my notes token')
    client.answer(client.wait_for_ask(forget, 'approval'), approved=True)
    assert client.wait_for_reply(forget) == 'Forgot `notes_token`.'
    assert client.http.get('/api/secrets').json() == []
