"""#131: a run left waiting on the user reminds them once for each thing it waits for, and when nobody answers in
time it stops by itself, frees its browser and says in the chat what it waited for.

The reminder sweep runs every second here, and a wait is worth a reminder after a second. Tests wait for the sweep to
have run (DBOS records each run of it), never for a fixed time.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import psycopg
import pytest
from conftest import Client, Human
from helpers import eventually
from sites.shop import Shop
from test_notifications import Mailbox, mailbox  # noqa: F401  # the fixture, and the SMTP server behind it

REMINDER = 'Subject: Sammy is still waiting for you'


@pytest.fixture
def ask_timeout() -> str:
    """How long a run waits for an answer: a day, as in production, unless a test sets it."""
    return str(24 * 60 * 60)


@pytest.fixture
def remind_after() -> str:
    return '1'


@pytest.fixture
def app_env(mailbox: Mailbox, ask_timeout: str, remind_after: str) -> dict[str, str]:  # noqa: F811
    return {
        'SMTP_URL': f'smtp://127.0.0.1:{mailbox.server_address[1]}',
        'ASK_TIMEOUT_SECONDS': ask_timeout,
        'REMIND_AFTER_SECONDS': remind_after,
        'REMINDER_CRON': '* * * * * *',  # every second
    }


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def reminders(mailbox: Mailbox) -> list[str]:  # noqa: F811
    return [message for message in mailbox.messages if REMINDER in message]


def database_now(database_url: str) -> datetime:
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT now()').fetchone()
    assert row is not None
    return row[0]


def wait_for_sweeps(database_url: str, since: datetime) -> None:
    """Until the reminder sweep has finished twice after `since`, on the database's clock."""

    def swept() -> bool | None:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                "SELECT count(*) FROM dbos.workflow_status WHERE name = 'sammy.remind_waiting' "
                "AND status = 'SUCCESS' AND created_at > %s",
                (since.timestamp() * 1000,),
            ).fetchone()
        assert row is not None
        return row[0] >= 2 or None

    eventually(swept, what='the reminder sweep to run twice more')


def test_one_reminder_for_each_wait(client: Client, shop: Shop, mailbox: Mailbox, database_url: str) -> None:  # noqa: F811
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    client.wait_for_ask(thread, 'handoff')
    eventually(lambda: reminders(mailbox) or None, what='a reminder about the hand-off')
    reminder = reminders(mailbox)[0]
    assert 'still waiting for you to take over its browser' in reminder and f'/#/t/{thread}' in reminder
    assert '/live/' not in reminder and 'sign in' not in reminder.lower()  # no hand-off link, no prompt
    (listed,) = client.http.get('/api/threads').json()
    assert (listed['status'], listed['waiting_for'], listed['waiting_long']) == ('waiting', 'handoff', True)
    wait_for_sweeps(database_url, database_now(database_url))
    assert len(reminders(mailbox)) == 1  # once, however often Sammy looks

    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    approval = client.wait_for_ask(thread, 'approval')  # a new wait: it gets a reminder of its own
    eventually(lambda: len(reminders(mailbox)) == 2 or None, what='a reminder about the approval')
    assert 'still waiting for your approval' in reminders(mailbox)[1]
    client.answer(approval, approved=True)
    assert '#1' in client.wait_for_reply(thread)
    wait_for_sweeps(database_url, database_now(database_url))
    assert len(reminders(mailbox)) == 2
    (listed,) = client.http.get('/api/threads').json()
    assert (listed['status'], listed['waiting_long']) == (None, False)


@pytest.mark.parametrize('remind_after', ['3'])
def test_no_reminder_once_the_user_answered(client: Client, mailbox: Mailbox, database_url: str) -> None:  # noqa: F811
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    question = client.wait_for_ask(thread, 'question')
    client.answer(question, text='green')
    assert 'green' in client.wait_for_reply(thread).lower()
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT created_at FROM sammy.asks WHERE id = %s', (question['id'],)).fetchone()
    assert row is not None
    wait_for_sweeps(database_url, row[0] + timedelta(seconds=3))  # sweeps that would have found it due
    assert reminders(mailbox) == []


@pytest.mark.parametrize('ask_timeout', ['3'])
def test_a_run_nobody_answers_stops_and_frees_its_browser(client: Client, shop: Shop, database_url: str) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    handoff = client.wait_for_ask(thread, 'handoff')
    run_id = client.thread(thread)['run']['id']

    eventually(lambda: client.thread(thread)['run']['status'] == 'stopped' or None, what='the run to stop')
    stopped = client.thread(thread)
    assert stopped['run']['ask'] is None
    assert stopped['messages'][-2:] == [
        {'role': 'event', 'text': f'Not answered in time: {handoff["prompt"]}'},
        {
            'role': 'assistant',
            'text': 'I stopped: I needed you to take over my browser, and nobody did in time. '
            'Send me a message here to pick it up again.',
        },
    ]
    with psycopg.connect(database_url) as connection:
        lease = connection.execute('SELECT run_id FROM sammy.jar_leases WHERE run_id = %s', (run_id,)).fetchone()
    assert lease is None  # the browser is free for the user's other chats
    (listed,) = client.http.get('/api/threads').json()
    assert (listed['status'], listed['outcome']) == (None, 'stopped')
    assert client.http.post(f'/api/asks/{handoff["id"]}', json={'done': True}).status_code == 409  # too late
    client.ask('Say hello.', thread)  # the chat takes a new message
    assert 'hello' in client.wait_for_reply(thread).lower()
