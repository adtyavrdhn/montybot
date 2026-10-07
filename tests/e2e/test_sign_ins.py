"""#4 and U2: a sign-in is saved for the user, encrypted, reused by the next run without asking, survives the app
being killed mid-run, and never reaches the app's logs."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from conftest import App, Client, Human
from helpers import eventually
from sites.shop import Shop


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def sign_in_through_hand_off(client: Client, thread: str) -> None:
    client.wait_for_ask(thread, 'handoff')
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')


@pytest.mark.u2
def test_a_sign_in_is_saved_and_reused(app: App, client: Client, shop: Shop, database_url: str) -> None:
    client.sign_up()
    first = client.ask(f'Order eggs from {shop.url}')
    sign_in_through_hand_off(client, first)
    client.answer(client.wait_for_ask(first, 'approval'), approved=True)
    assert '#1' in client.wait_for_reply(first)

    # A new conversation, later: no hand-off this time, straight to the approval.
    second = client.ask(f'Order eggs from {shop.url}')
    client.answer(client.wait_for_ask(second, 'approval'), approved=True)
    assert '#2' in client.wait_for_reply(second)
    assert shop.sign_ins == 1
    assert [o.user for o in shop.orders] == ['alice', 'alice']

    # A visible reply means the browser's lease is already free, not pending workflow cleanup.
    run_id = client.thread(second)['run']['id']
    with psycopg.connect(database_url) as connection:
        lease = connection.execute('SELECT run_id FROM montybot.jar_leases WHERE run_id = %s', (run_id,)).fetchone()
    assert lease is None

    # Saved encrypted: the session cookie's value is nowhere in the row.
    (sid,) = shop.sessions
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT state, version FROM montybot.sign_ins').fetchone()
    assert row is not None and sid.encode() not in bytes(row[0]) and b'127.0.0.1' not in bytes(row[0])

    assert client.http.get('/api/sign-ins').json() == [{'site': '127.0.0.1'}]

    # run.finish publishes the reply before run.close saves the browser and releases its lease.
    # A 409 during that cleanup is expected; wait for forgetting to succeed, not an arbitrary delay.
    def forgotten() -> bool | None:
        response = client.http.delete('/api/sign-ins/127.0.0.1')
        if response.status_code == 409:
            return None
        assert response.status_code == 200, response.text
        return True

    eventually(forgotten, what='the completed run to release its sign-in lease')
    assert client.http.get('/api/sign-ins').json() == []

    # Forgotten: the next order needs the user to sign in again.
    third = client.ask(f'Order eggs from {shop.url}')
    sign_in_through_hand_off(client, third)
    client.answer(client.wait_for_ask(third, 'approval'), approved=False)
    client.wait_for_reply(third)
    assert shop.sign_ins == 2

    log = app.log.read_text()
    assert 'hunter2' not in log and sid not in log


@pytest.mark.u2
def test_the_app_is_killed_during_the_hand_off(app: App, client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    handoff = client.wait_for_ask(thread, 'handoff')

    app.kill()
    app.start()  # the browser service lost the hand-off; the live view starts a new one on the saved page

    assert client.wait_for_ask(thread, 'handoff')['id'] == handoff['id']
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    client.answer(client.wait_for_ask(thread, 'approval'), approved=True)
    assert '#1' in client.wait_for_reply(thread)
    assert len(shop.orders) == 1


@pytest.mark.u3
def test_the_app_is_killed_while_an_approval_waits(app: App, client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    sign_in_through_hand_off(client, thread)
    approval = client.wait_for_ask(thread, 'approval')
    # The run holds the user's sign-ins while it waits, so they cannot be changed under it.
    assert client.http.delete('/api/sign-ins/127.0.0.1').status_code == 409

    app.kill()
    app.start()  # replayed steps return their recorded pages; the browser reopens signed in, on the cart

    again = client.wait_for_ask(thread, 'approval')
    assert again['id'] == approval['id']
    client.answer(again, approved=True)
    assert '#1' in client.wait_for_reply(thread)
    assert [(o.user, o.items) for o in shop.orders] == [('alice', ['eggs'])]
    assert shop.sign_ins == 1
