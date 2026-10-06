"""#3: the app on DBOS. A message gets a reply; a run pauses for the user and survives the app being killed while it
waits; an approval gates an irreversible click, and a hand-off gives the user the run's own browser."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import App, Client, Human
from sites.shop import Shop


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def test_a_message_gets_a_reply(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Say hello.')
    assert client.wait_for_reply(thread) == 'Hello! I am monty-bot.'


def test_a_waiting_run_survives_the_app_being_killed(app: App, client: Client) -> None:
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    question = client.wait_for_ask(thread, 'question')
    assert question['prompt'] == 'What is your favourite colour?'

    app.kill()
    app.start()  # DBOS finds the unfinished workflow and runs it up to the same wait

    assert client.wait_for_ask(thread, 'question')['id'] == question['id']
    client.answer(question, text='green')
    assert client.wait_for_reply(thread) == 'Got it: your favourite colour is green.'
    memories = client.http.get('/api/memories').json()
    assert [m['text'] for m in memories] == ['Favourite colour: green']


def test_sign_in_through_a_hand_off_then_approve_the_order(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')

    handoff = client.wait_for_ask(thread, 'handoff')
    assert handoff['prompt'] == 'Please sign in to the shop, then hand the browser back.'
    human = Human(client, client.thread(thread)['run']['id'])
    human.sign_in('alice', 'hunter2')
    client.answer(handoff, done=True)

    approval = client.wait_for_ask(thread, 'approval')
    assert approval['prompt'] == 'Place the order for eggs ($3.20)'
    assert shop.orders == []
    client.answer(approval, approved=True)

    assert client.wait_for_reply(thread) == 'Done. Order #1: eggs, $3.20'
    assert [(o.user, o.items) for o in shop.orders] == [('alice', ['eggs'])]


def test_a_denied_approval_places_no_order(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    handoff = client.wait_for_ask(thread, 'handoff')
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    client.answer(handoff, done=True)
    client.answer(client.wait_for_ask(thread, 'approval'), approved=False, reason='too pricey')

    assert client.wait_for_reply(thread) == 'I did not place the order. The user said no: too pricey'
    assert shop.orders == []


def test_users_cannot_see_each_other(app: App, client: Client) -> None:
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    question = client.wait_for_ask(thread, 'question')

    run_id = client.thread(thread)['run']['id']
    with httpx.Client(base_url=app.url) as other:
        signed_up = other.post('/api/signup', json={'email': 'mallory@example.test', 'password': 'correct horse'})
        assert signed_up.status_code == 201
        assert other.get(f'/api/threads/{thread}').status_code == 404
        assert other.get(f'/api/runs/{run_id}').status_code == 404
        assert other.get(f'/api/runs/{run_id}/screen').status_code == 404
        assert other.post(f'/api/runs/{run_id}/screen', json={'kind': 'press', 'key': 'Enter'}).status_code == 404
        assert other.post(f'/api/asks/{question["id"]}', json={'text': 'red'}).status_code == 404
        assert other.post(f'/api/threads/{thread}/messages', json={'text': 'hi'}).status_code == 404
        assert other.get('/api/threads').json() == []
    client.answer(question, text='blue')
    assert client.wait_for_reply(thread) == 'Got it: your favourite colour is blue.'


def test_a_failed_run_says_so_and_frees_the_thread(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Fail please.')
    assert client.wait_for_reply(thread).startswith('Something went wrong')
    client.ask('Say hello.', thread)
    assert client.wait_for_reply(thread) == 'Hello! I am monty-bot.'


def test_writes_must_be_json(app: App) -> None:
    with httpx.Client(base_url=app.url) as browser:
        # what a form on another site can send
        body = '{"email": "victim@example.test", "password": "correct horse"}'
        refused = browser.post('/api/signup', content=body, headers={'content-type': 'text/plain'})
        assert refused.status_code == 415
        assert 'montybot_session' not in refused.cookies
        assert browser.post('/api/threads', json={'text': '   '}).status_code == 401
        browser.post('/api/signup', content=body, headers={'content-type': 'application/json'})
        assert browser.post('/api/threads', json={'text': '   '}).status_code == 422
