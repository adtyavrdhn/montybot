"""#9 and U4: schedules the user sets up in chat, run by DBOS's scheduler, paused and deleted from the web app and from
chat; a weekly cart fill across two weeks, a slot watch that notifies once, and a schedule that outlives the app.

Weeks pass without waiting for them: `DBOS.trigger_schedule` enqueues an occurrence now, through the same function
the DBOS scheduler calls at the cron's times (`dbos._scheduler._enqueue_scheduled_workflow`). The last test waits for
the scheduler itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from conftest import App, Client, Human
from dbos import DBOSClient, WorkflowHandle
from helpers import eventually
from sites.shop import Shop
from sites.slots import Slots
from test_notifications import Mailbox, mailbox  # noqa: F401  # the fixture, and the SMTP server behind it

SLOT = 'Tuesday 18:00-19:00'


@pytest.fixture
def app_env(mailbox: Mailbox) -> dict[str, str]:  # noqa: F811
    return {'SMTP_URL': f'smtp://127.0.0.1:{mailbox.server_address[1]}'}


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def slots() -> Iterator[Slots]:
    site = Slots()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def dbos(app: App, database_url: str) -> Iterator[DBOSClient]:
    client = DBOSClient(system_database_url=database_url)
    yield client
    client.destroy()


def set_up(client: Client, message: str) -> dict[str, Any]:
    """Ask for a schedule in chat and approve it; returns it as the Schedules page shows it."""
    chat = client.ask(message)
    approval = client.wait_for_ask(chat, 'approval')
    client.answer(approval, approved=True)
    assert 'Scheduled' in client.wait_for_reply(chat)
    (schedule,) = client.http.get('/api/schedules').json()
    return schedule


def schedules(client: Client) -> list[dict[str, Any]]:
    response = client.http.get('/api/schedules')
    assert response.status_code == 200, response.text
    return response.json()


def fire(dbos: DBOSClient, schedule: dict[str, Any]) -> WorkflowHandle[None]:
    """One occurrence, now."""
    return dbos.trigger_schedule(f'sammy-schedule-{schedule["id"]}')


def finished(handle: WorkflowHandle[None]) -> None:
    status = eventually(
        lambda: s if (s := handle.get_status().status) not in ('ENQUEUED', 'PENDING') else None,
        what='the occurrence to finish',
    )
    assert status == 'SUCCESS'


def runs_in(database_url: str, thread_id: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT count(*) FROM sammy.runs WHERE thread_id = %s AND trigger = 'schedule'", (thread_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def reply_of(client: Client, thread_id: str) -> str:
    thread = client.thread(thread_id)
    assert thread['run']['status'] == 'done', thread
    return thread['messages'][-1]['text']


def mails(mailbox: Mailbox, text: str) -> int:  # noqa: F811
    return sum(text in message for message in mailbox.messages)


@pytest.mark.u4
def test_a_weekly_cart_fill_over_two_weeks(
    app: App,
    client: Client,
    shop: Shop,
    mailbox: Mailbox,  # noqa: F811
    dbos: DBOSClient,
    database_url: str,
) -> None:
    client.sign_up()
    chat = client.ask(f'Every Monday at 9, fill my cart at {shop.url} with eggs and milk')
    approval = client.wait_for_ask(chat, 'approval')
    assert 'Weekly groceries' in approval['prompt'] and 'Mondays at 09:00' in approval['prompt']
    client.answer(approval, approved=True)
    assert 'Scheduled' in client.wait_for_reply(chat)
    (weekly,) = schedules(client)
    assert weekly['name'] == 'Weekly groceries' and not weekly['paused']
    assert weekly['when'] == 'Mondays at 09:00 (Europe/London)'
    thread = weekly['thread_id']

    # Week 1: no saved sign-in yet, so the run hands off and the user signs in.
    week_1 = fire(dbos, weekly)
    eventually(lambda: client.thread(thread)['run'], what='the first run')
    client.wait_for_ask(thread, 'handoff')
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    finished(week_1)
    assert reply_of(client, thread) == 'In cart: eggs, milk'
    assert mails(mailbox, 'finished a scheduled task') == 1

    # The groceries came; week 2 reuses the saved sign-in, with nobody there.
    shop.carts.clear()
    week_2 = fire(dbos, weekly)
    finished(week_2)
    assert reply_of(client, thread) == 'In cart: eggs, milk'
    assert shop.sign_ins == 1 and shop.carts == {'alice': ['eggs', 'milk']}
    assert runs_in(database_url, thread) == 2
    assert mails(mailbox, 'finished a scheduled task') == 2
    assert mails(mailbox, 'take over its browser') == 1

    # Another user cannot see, pause, resume or delete it.
    other = Client(app)
    other.sign_up()
    assert schedules(other) == []
    for path in ('pause', 'resume'):
        assert other.http.post(f'/api/schedules/{weekly["id"]}/{path}', json={}).status_code == 404
    assert other.http.delete(f'/api/schedules/{weekly["id"]}').status_code == 404
    other.http.close()

    # Paused in the web app: an occurrence does nothing.
    assert client.http.post(f'/api/schedules/{weekly["id"]}/pause', json={}).json()['paused'] is True
    assert schedules(client)[0]['paused'] is True
    finished(fire(dbos, weekly))
    assert runs_in(database_url, thread) == 2

    # Resumed and paused again from chat.
    assert 'Resumed' in client.wait_for_reply(client.ask(f'Resume the schedule {weekly["id"]}'))
    assert schedules(client)[0]['paused'] is False
    assert 'Paused' in client.wait_for_reply(client.ask(f'Pause the schedule {weekly["id"]}'))
    assert schedules(client)[0]['paused'] is True

    assert client.http.delete(f'/api/schedules/{weekly["id"]}').status_code == 200
    assert schedules(client) == []
    assert dbos.list_schedules(schedule_name_prefix='sammy-schedule-') == []


def approvals_in(database_url: str, thread_id: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            'SELECT count(*) FROM sammy.asks a JOIN sammy.runs r ON r.id = a.run_id '
            "WHERE r.thread_id = %s AND a.kind = 'approval'",
            (thread_id,),
        ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.u4
def test_a_scheduled_order_is_placed_without_asking_again(
    client: Client, shop: Shop, dbos: DBOSClient, database_url: str
) -> None:
    """The user approves the schedule, order included, once. Its runs then place the order with nobody there."""
    client.sign_up()
    chat = client.ask(f'Every Friday at 8, order eggs from {shop.url}')
    approval = client.wait_for_ask(chat, 'approval')
    assert 'without asking you again' in approval['prompt']
    client.answer(approval, approved=True)
    assert 'Scheduled' in client.wait_for_reply(chat)
    (weekly,) = schedules(client)
    thread = weekly['thread_id']

    # Week 1 signs in through a hand-off; week 2 has the saved sign-in. Neither asks to place the order.
    for week in (1, 2):
        handle = fire(dbos, weekly)
        if week == 1:
            eventually(lambda: client.thread(thread)['run'], what='the first run')
            client.wait_for_ask(thread, 'handoff')
            Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
        finished(handle)
        assert reply_of(client, thread).startswith(f'Done. Order #{week}: eggs')
    assert len(shop.orders) == 2 and approvals_in(database_url, thread) == 0

    # Asked in chat, the same order still waits for the user's yes.
    chat = client.ask(f'Order eggs from {shop.url}')
    client.wait_for_ask(chat, 'approval')
    assert len(shop.orders) == 2


@pytest.mark.u4
def test_a_slot_watch_notifies_once(
    client: Client,
    slots: Slots,
    mailbox: Mailbox,  # noqa: F811
    dbos: DBOSClient,
    database_url: str,
) -> None:
    client.sign_up()
    watch = set_up(client, f'Tell me when a delivery slot opens at {slots.url}')
    assert watch['watch'] is True
    thread = watch['thread_id']

    for _ in range(2):
        finished(fire(dbos, watch))
        assert reply_of(client, thread) == 'Not yet.'
    assert mails(mailbox, 'Sammy found') == 0 and mails(mailbox, 'Sammy finished') == 0

    slots.open_slot = SLOT
    finished(fire(dbos, watch))
    assert SLOT in reply_of(client, thread)
    assert mails(mailbox, 'found what you asked it to watch for') == 1
    assert mails(mailbox, SLOT) == 0  # what it found stays in the chat
    assert schedules(client)[0]['paused'] is True

    # Later occurrences (one enqueued before the pause, say) do not tell the user again.
    finished(fire(dbos, watch))
    assert runs_in(database_url, thread) == 3
    assert mails(mailbox, 'Sammy found') == 1 and mails(mailbox, 'Sammy finished') == 0

    assert client.wait_for_reply(client.ask(f'Delete the schedule {watch["id"]}')) == 'Deleted.'
    assert schedules(client) == []
    assert dbos.list_schedules(schedule_name_prefix='sammy-schedule-') == []
    assert reply_of(client, thread)  # it ran, so its chat stays, with what it found


@pytest.mark.u4
def test_deleting_a_schedule_that_never_ran_takes_its_empty_chat(client: Client, slots: Slots) -> None:
    client.sign_up()
    watch = set_up(client, f'Tell me when a delivery slot opens at {slots.url}')
    assert watch['thread_id'] in [t['id'] for t in client.http.get('/api/threads').json()]
    assert client.http.delete(f'/api/schedules/{watch["id"]}').status_code == 200
    assert watch['thread_id'] not in [t['id'] for t in client.http.get('/api/threads').json()]


@pytest.mark.u4
def test_a_schedule_outlives_the_app(app: App, client: Client, slots: Slots, database_url: str) -> None:
    client.sign_up()
    watch = set_up(client, f'Check every minute for a delivery slot at {slots.url}')

    app.stop()
    app.start()

    assert [s['id'] for s in schedules(client)] == [watch['id']]
    # DBOS's own scheduler fires it, at the next minute.
    eventually(lambda: runs_in(database_url, watch['thread_id']) or None, timeout=150, what='the scheduler to fire it')
    eventually(lambda: client.thread(watch['thread_id'])['run']['status'] == 'done' or None, what='the run')
    assert reply_of(client, watch['thread_id']) == 'Not yet.'
    assert client.http.delete(f'/api/schedules/{watch["id"]}').status_code == 200


def test_deleting_a_schedules_chat_deletes_the_schedule(client: Client, slots: Slots, dbos: DBOSClient) -> None:
    client.sign_up()
    watch = set_up(client, f'Tell me when a delivery slot opens at {slots.url}')
    assert len(dbos.list_schedules(schedule_name_prefix='sammy-schedule-')) == 1

    assert client.http.delete(f'/api/threads/{watch["thread_id"]}').status_code == 200
    assert schedules(client) == []
    assert dbos.list_schedules(schedule_name_prefix='sammy-schedule-') == []  # nothing fires for a chat that is gone
