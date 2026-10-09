"""#132: subagents. A task that splits by site runs as jobs side by side, each a run of its own in a tab of the
user's browser, and the parent's context gets only their answers. A job's asks and hand-offs are the parent thread's,
a job waiting for the user survives the app being killed, and stopping the parent stops its jobs."""

from __future__ import annotations

import io
import time
from collections.abc import Iterator

import psycopg
import pytest
from conftest import App, Client, Human
from PIL import Image
from sites.prices import KettleShop
from sites.shop import Shop

pytestmark = pytest.mark.scripted


@pytest.fixture
def app_env(request: pytest.FixtureRequest) -> dict[str, str]:
    """Subagents need an engine with tabs, where a user's runs share one browser: the fake engine gets them."""
    browser = str(request.config.getoption('--browser'))
    if browser == 'fake':
        return {'BROWSER_BACKEND': 'sites.html_browser:new_tabbed_backend'}
    if browser not in ('cdp', 'cloak'):
        pytest.skip(f'subagents need a browser engine with tabs, which {browser} does not have')
    return {}


@pytest.fixture
def shops() -> Iterator[list[KettleShop]]:
    sites = [
        KettleShop(name='Kettle World', price=34.99),
        KettleShop(name='Pot Palace', price=29.50),
        KettleShop(name='Boil Barn', price=31.25),
    ]
    for site in sites:
        site.start()
    yield sites
    for site in sites:
        site.stop()


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def reply_in_seconds(client: Client, text: str) -> tuple[str, str, float]:
    """The thread, its reply, and how long the user waited for it."""
    started = time.monotonic()
    thread = client.ask(text)
    reply = client.wait_for_reply(thread)
    return thread, reply, time.monotonic() - started


def stored_context(database_url: str, thread_id: str) -> str:
    """Everything the thread's runs put in the model's context, as stored for its next message."""
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            'SELECT payload::text FROM sammy.messages WHERE thread_id = %s ORDER BY position', (thread_id,)
        ).fetchall()
    return '\n'.join(row[0] for row in rows)


def test_three_sites_side_by_side_beat_one_at_a_time(
    client: Client, shops: list[KettleShop], database_url: str
) -> None:
    client.sign_up()
    client.wait_for_reply(client.ask('Say hello.'))  # a user's first run pays for things neither version should
    sites = ' '.join(site.url for site in shops)
    # Side by side first, so it, not the one at a time version, pays for anything else starting cold.
    parallel, side_by_side, parallel_seconds = reply_in_seconds(client, f'Side by side, compare the kettle at {sites}')
    sequential, one_by_one, sequential_seconds = reply_in_seconds(
        client, f'One site at a time, compare the kettle at {sites}'
    )

    assert side_by_side == one_by_one == f'Cheapest: {shops[1].url} at $29.50'
    assert [site.visits for site in shops] == [2, 2, 2]  # each version read each site once
    assert parallel_seconds < sequential_seconds, (parallel_seconds, sequential_seconds)
    # The parent got three short answers, not three product pages.
    parallel_context = stored_context(database_url, parallel)
    sequential_context = stored_context(database_url, sequential)
    assert 'Review 1:' in sequential_context
    assert 'Review 1:' not in parallel_context
    assert len(parallel_context) < len(sequential_context) / 2, (len(parallel_context), len(sequential_context))


def statuses(client: Client) -> dict[str, tuple[str | None, str | None]]:
    """What the chat list shows for each thread: its status, and what it waits for."""
    return {t['id']: (t['status'], t['waiting_for']) for t in client.http.get('/api/threads').json()}


def pixels(png: bytes) -> tuple[tuple[int, int], bytes]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size, image.convert('RGB').tobytes()


def test_a_jobs_question_is_asked_in_the_parent_thread_and_survives_a_restart(
    app: App, client: Client, shops: list[KettleShop], database_url: str
) -> None:
    client.sign_up()
    thread = client.ask(f'Side by side, ask me which kettle at {shops[0].url}')
    question = client.wait_for_ask(thread, 'question')
    assert question['prompt'] == 'Which kettle do you mean?'
    assert statuses(client) == {thread: ('waiting', 'question')}

    # The parent's live screen is the job's tab, open on its page while it waits: the parent has no tab of its own.
    parent = client.thread(thread)['run']['id']
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT id FROM sammy.runs WHERE parent_run_id = %s', (parent,)).fetchone()
    assert row is not None
    job = row[0]
    screen = client.http.get(f'/api/runs/{parent}/screen')
    assert screen.status_code == 200, screen.status_code
    assert screen.headers['content-type'] == 'image/png'
    assert pixels(screen.content) == pixels(client.http.get(f'/api/runs/{job}/screen').content)

    app.kill()
    app.start()  # DBOS resumes the parent's workflow and the job's, which waits for the same answer

    assert client.wait_for_ask(thread, 'question')['id'] == question['id']
    client.answer(question, text='Acme kettle')
    reply = client.wait_for_reply(thread)
    assert reply.endswith(f'Answer: The Acme kettle at {shops[0].url} is $34.99'), reply
    assert client.thread(thread)['messages'][1:3] == [
        {'role': 'assistant', 'text': 'Which kettle do you mean?'},
        {'role': 'user', 'text': 'Acme kettle'},
    ]
    assert statuses(client) == {thread: (None, None)}


def test_a_jobs_hand_off_is_taken_over_from_the_parent_thread(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Have a helper put eggs in my cart at {shop.url}')
    handoff = client.wait_for_ask(thread, 'handoff')
    assert 'sign in' in handoff['prompt'].lower()

    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')  # the job's tab, from the parent

    assert client.wait_for_reply(thread).endswith('Answer: In cart: eggs')
    assert shop.carts == {'alice': ['eggs']}
    events = [m['text'] for m in client.thread(thread)['messages'] if m['role'] == 'event']
    assert events == [f'You took over the browser: {handoff["prompt"]}']


def test_stopping_the_parent_stops_its_jobs(client: Client, shops: list[KettleShop], database_url: str) -> None:
    client.sign_up()
    thread = client.ask(f'Side by side, ask me which kettle at {shops[0].url}')
    question = client.wait_for_ask(thread, 'question')
    run_id = client.thread(thread)['run']['id']

    assert client.http.post(f'/api/runs/{run_id}/stop', json={}).status_code == 200

    stopped = client.thread(thread)
    assert stopped['run']['status'] == 'stopped'
    assert stopped['run']['ask'] is None
    assert client.http.post(f'/api/asks/{question["id"]}', json={'text': 'Acme'}).status_code == 409  # too late
    with psycopg.connect(database_url) as connection:
        jobs = connection.execute('SELECT status FROM sammy.runs WHERE parent_run_id = %s', (run_id,)).fetchall()
    assert jobs == [('stopped',)]
    assert statuses(client) == {thread: (None, None)}
    client.ask('Say hello.', thread)  # the thread and the browser are free
    assert 'hello' in client.wait_for_reply(thread).lower()
