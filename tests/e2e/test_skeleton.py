"""#3: the app on DBOS. A message gets a reply; a run pauses for the user and survives the app being killed while it
waits; an approval gates an irreversible click, and a hand-off gives the user the run's own browser."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import httpx
import pytest
from conftest import App, Client, Human
from sites.shop import Shop

from montybot.liveview.client import LiveViewClient, LiveViewClosed


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def test_a_message_gets_a_reply(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Say hello.')
    assert 'hello' in client.wait_for_reply(thread).lower()


def test_a_waiting_run_survives_the_app_being_killed(app: App, client: Client) -> None:
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    question = client.wait_for_ask(thread, 'question')
    assert 'colour' in question['prompt'].lower() or 'color' in question['prompt'].lower()

    app.kill()
    app.start()  # DBOS finds the unfinished workflow and runs it up to the same wait

    assert client.wait_for_ask(thread, 'question')['id'] == question['id']
    client.answer(question, text='green')
    assert 'green' in client.wait_for_reply(thread).lower()
    assert client.thread(thread)['messages'][1:3] == [
        {'role': 'assistant', 'text': question['prompt']},
        {'role': 'user', 'text': 'green'},
    ]
    memories = client.http.get('/api/memories').json()
    assert len(memories) == 1 and 'green' in memories[0]['text'].lower()  # written once, after the restart


def test_sign_in_through_a_hand_off_then_approve_the_order(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')

    handoff = client.wait_for_ask(thread, 'handoff')
    assert 'sign in' in handoff['prompt'].lower()
    human = Human(client, client.thread(thread)['run']['id'])
    human.sign_in('alice', 'hunter2')

    approval = client.wait_for_ask(thread, 'approval')
    assert 'eggs' in approval['prompt'].lower()
    assert shop.orders == []
    client.answer(approval, approved=True)

    assert '#1' in client.wait_for_reply(thread)
    assert [(o.user, o.items) for o in shop.orders] == [('alice', ['eggs'])]
    events = [m['text'] for m in client.thread(thread)['messages'] if m['role'] == 'event']
    assert events == [f'You took over the browser: {handoff["prompt"]}', f'You approved: {approval["prompt"]}']


def test_a_denied_approval_places_no_order(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    client.wait_for_ask(thread, 'handoff')
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    client.answer(client.wait_for_ask(thread, 'approval'), approved=False, reason='too pricey')

    assert client.wait_for_reply(thread)
    assert shop.orders == []


def statuses(client: Client) -> dict[str, str | None]:
    """The status the chat list shows for each thread."""
    return {thread['id']: thread['status'] for thread in client.http.get('/api/threads').json()}


def test_stop_a_run_that_waits_for_the_user(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Ask me my favourite colour and remember it.')
    client.wait_for_ask(thread, 'question')
    assert statuses(client) == {thread: 'waiting'}
    run_id = client.thread(thread)['run']['id']

    assert client.http.post(f'/api/runs/{run_id}/stop', json={}).status_code == 200

    stopped = client.thread(thread)
    assert stopped['run']['status'] == 'stopped'
    assert stopped['run']['ask'] is None
    assert stopped['messages'][-1] == {'role': 'assistant', 'text': 'You stopped this.'}
    assert statuses(client) == {thread: None}
    assert client.http.post(f'/api/runs/{run_id}/stop', json={}).status_code == 409
    client.ask('Say hello.', thread)  # the thread takes a new message
    assert 'hello' in client.wait_for_reply(thread).lower()


@pytest.mark.scripted
def test_the_agent_knows_the_time_for_the_user(client: Client) -> None:
    client.sign_up()
    created = client.http.post('/api/threads', json={'text': 'What time is it for me?', 'timezone': 'Asia/Tokyo'})
    assert '(Asia/Tokyo)' in client.wait_for_reply(created.json()['thread_id'])
    created = client.http.post('/api/threads', json={'text': 'What time is it for me?', 'timezone': 'Mars/Base'})
    assert '(Asia/Tokyo)' in client.wait_for_reply(created.json()['thread_id'])  # not a real zone: the last one


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
        assert other.post(f'/api/runs/{run_id}/live', json={}).status_code == 404
        assert other.post(f'/api/asks/{question["id"]}', json={'text': 'red'}).status_code == 404
        assert other.post(f'/api/threads/{thread}/messages', json={'text': 'hi'}).status_code == 404
        assert other.post(f'/api/runs/{run_id}/stop', json={}).status_code == 404
        assert other.get('/api/threads').json() == []
    assert client.http.post(f'/api/asks/{question["id"]}', json={'text': '  '}).status_code == 422
    client.answer(question, text='blue')
    assert 'blue' in client.wait_for_reply(thread).lower()


@pytest.mark.scripted
def test_a_failed_run_says_so_and_frees_the_thread(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Fail please.')
    assert client.wait_for_reply(thread, failed=True).startswith('Something went wrong')
    client.ask('Say hello.', thread)
    assert 'hello' in client.wait_for_reply(thread).lower()


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


@pytest.mark.u3
@pytest.mark.scripted
def test_code_cannot_skip_the_approval(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs straight from code at {shop.url}')
    client.wait_for_ask(thread, 'handoff')
    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    reply = client.wait_for_reply(thread)
    assert 'Use the `commit` tool' in reply
    assert shop.orders == []


def test_only_the_runs_user_can_take_over(app: App, client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')
    client.wait_for_ask(thread, 'handoff')
    run_id = client.thread(thread)['run']['id']
    assert client.http.get(f'/api/runs/{run_id}/live').status_code == 405
    link = client.http.post(f'/api/runs/{run_id}/live', json={}).json()['url']
    assert client.http.get(link).status_code == 200

    with httpx.Client(base_url=app.url) as other:
        assert other.get(link).status_code == 401  # signed out
        other.post('/api/signup', json={'email': 'mallory@example.test', 'password': 'correct horse'})
        assert other.get(link).status_code == 404
        session = other.cookies.get('montybot_session')

    async def connect() -> int | None:
        url = app.url.replace('http', 'ws', 1) + link + '/ws'
        async with LiveViewClient.connect(url, session=session, origin=app.url) as live:
            with contextlib.suppress(LiveViewClosed, TimeoutError):
                await live.next_frame(timeout=5)
            return live.close_code

    assert asyncio.run(connect()) == 4404
