"""#125: a message sent while a run works joins that run. The run's next model request has it, it survives the app
being killed, and it never starts a second run beside the first. One the run never read starts the next run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from conftest import App, Client, Human
from helpers import eventually
from sites.shop import Shop

CHANGE = 'Actually, make it milk, not eggs.'


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def cue(tmp_path: Path) -> Path:
    """Where the `Say hello on cue` script says it has started, and waits for the test's go-ahead."""
    path = tmp_path / 'cue'
    path.mkdir()
    return path


@pytest.fixture
def app_env(cue: Path) -> dict[str, str]:
    return {'SAMMY_TEST_CUE': str(cue)}


def runs_in(database_url: str, thread_id: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT count(*) FROM sammy.runs WHERE thread_id = %s', (thread_id,)).fetchone()
    assert row is not None
    return row[0]


def steer(client: Client, thread_id: str, text: str) -> str:
    """Send a message while the thread's run works; returns the id of the run it joined."""
    response = client.http.post(f'/api/threads/{thread_id}/messages', json={'text': text})
    assert response.status_code == 201, response.text
    assert response.json()['steered'] is True
    return response.json()['run_id']


@pytest.mark.u3
@pytest.mark.scripted
def test_a_message_sent_mid_run_changes_the_order(app: App, client: Client, shop: Shop, database_url: str) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    handoff = client.wait_for_ask(thread, 'handoff')
    run_id = client.thread(thread)['run']['id']

    assert steer(client, thread, CHANGE) == run_id
    shown = client.thread(thread)
    assert shown['messages'][-1] == {'role': 'user', 'text': CHANGE, 'unread': True}  # "Sammy will see this next"
    assert shown['run']['unread'] == 1

    app.kill()  # the message is in the database, not in the app
    app.start()

    assert client.wait_for_ask(thread, 'handoff')['id'] == handoff['id']
    Human(client, run_id).sign_in('alice', 'hunter2')
    approval = client.wait_for_ask(thread, 'approval')
    assert 'milk' in approval['prompt'].lower()  # the next model request had the message
    assert client.thread(thread)['run']['unread'] == 0

    app.kill()  # replayed, the run reads the same message at the same request
    app.start()

    assert client.wait_for_ask(thread, 'approval')['id'] == approval['id']
    client.answer(approval, approved=True)
    assert '#1' in client.wait_for_reply(thread)
    assert [(o.user, o.items) for o in shop.orders] == [('alice', ['milk'])]
    assert runs_in(database_url, thread) == 1
    assert client.thread(thread)['messages'][:4] == [
        {'role': 'user', 'text': f'Order eggs from {shop.url}'},
        {'role': 'event', 'text': f'You took over the browser: {handoff["prompt"]}'},
        {'role': 'user', 'text': CHANGE},  # where it was sent: after the hand-off, before the approval
        {'role': 'event', 'text': f'You approved: {approval["prompt"]}'},
    ]


@pytest.mark.scripted
def test_a_message_the_run_did_not_read_starts_the_next_run(client: Client, cue: Path, database_url: str) -> None:
    client.sign_up()
    thread = client.ask('Say hello on cue.')
    eventually(lambda: (cue / 'started').exists() or None, what='the run to make its last model request')
    first = client.thread(thread)['run']['id']
    assert steer(client, thread, 'Say hello.') == first
    (cue / 'go').touch()

    assert 'hello' in client.wait_for_reply(thread).lower()
    shown = client.thread(thread)
    assert shown['run']['id'] != first and shown['run']['prompt'] == 'Say hello.'
    assert [m['text'] for m in shown['messages']] == [
        'Say hello on cue.',
        'Hello on cue.',
        'Say hello.',
        shown['run']['output'],
    ]
    assert runs_in(database_url, thread) == 2


def test_a_stopped_run_starts_nothing_with_what_it_did_not_read(client: Client, database_url: str) -> None:
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    client.wait_for_ask(thread, 'question')
    run_id = steer(client, thread, 'Say hello.')

    assert client.http.post(f'/api/runs/{run_id}/stop', json={}).status_code == 200
    assert client.thread(thread)['messages'] == [
        {'role': 'user', 'text': 'Ask me my favourite colour and remember it.'},
        {'role': 'user', 'text': 'Say hello.'},  # no longer "Sammy will see this next"
        {'role': 'assistant', 'text': 'You stopped this.'},
    ]
    assert runs_in(database_url, thread) == 1
