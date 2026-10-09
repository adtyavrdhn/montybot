"""#128: skills. The user teaches Sammy a task in the live view; the lesson becomes a draft skill that holds no
password; once the user saves it, the next run on that site takes fewer model calls. Sammy can also save a skill
itself, with the user's approval, and the apps' API edits and deletes them."""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import urlsplit

import psycopg
import pytest
from conftest import App, Client, Human
from sites.shop import PASSWORD, Order, Shop

from sammy.browser.contract import Press, Type
from sammy.liveview.client import LiveViewClient
from sammy.liveview.wire import Taught

GOAL = 'Reorder my last order'


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def model_calls(database_url: str, thread_id: str) -> int:
    """How many responses the model gave in the thread: one per model request."""
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT count(*) FROM sammy.messages WHERE thread_id = %s AND payload->>'kind' = 'response'", (thread_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def teach(client: Client, thread: str) -> Taught:
    """The user takes over, presses Teach Sammy, signs in and reorders their last order, then stops: by keyboard,
    as the scripted human always drives, a character at a time as the live view's page sends typing."""
    client.wait_for_ask(thread, 'handoff')
    drafted: list[Taught] = []

    async def lesson(live: LiveViewClient) -> None:
        await live.wait_for_url(lambda url: '/login' in url)
        await live.teach(GOAL)
        await live.send(Type(text='alice'))
        await live.send(Press(key='Tab'))
        for character in PASSWORD:
            await live.send(Type(text=character))
        await live.send(Press(key='Enter'))
        await live.wait_for_url(lambda url: urlsplit(url).path == '/orders')  # they look before they go on
        await live.send(Press(key='Tab'))  # to the newest order's Reorder
        await live.send(Press(key='Enter'))
        await live.wait_for_url(lambda url: urlsplit(url).path == '/cart')
        drafted.append(await live.stop_teaching())

    Human(client, client.thread(thread)['run']['id']).drive(lesson)
    return drafted[0]


@pytest.mark.scripted
def test_a_taught_skill_makes_the_next_run_shorter(app: App, client: Client, shop: Shop, database_url: str) -> None:
    client.sign_up()
    shop.orders.append(Order(1, 'alice', ['eggs', 'milk'], 4.30))

    lesson = client.ask(f'Show me how to reorder my last order at {shop.url}')
    taught = teach(client, lesson)
    assert 'Skills' in client.wait_for_reply(lesson)
    assert taught.name == GOAL
    assert shop.carts == {'alice': ['eggs', 'milk']}

    # A draft, for the user to review: what they typed into the username field is in it, the password is not.
    (draft,) = client.http.get('/api/skills').json()
    assert draft['id'] == taught.skill_id and draft['draft'] is True
    assert 'Typed "alice" into textbox "username"' in draft['steps']
    assert 'Typed a secret into textbox "password"' in draft['steps']
    assert 'Pressed Enter on button "Reorder"' in draft['steps']
    assert PASSWORD not in json.dumps(draft)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute('SELECT * FROM sammy.skills').fetchall()
    assert PASSWORD not in repr(rows)
    assert PASSWORD not in app.log.read_text()

    # Not saved yet, so Sammy finds the way itself, a page at a time.
    shop.carts.clear()
    without = client.ask(f'Reorder my last order at {shop.url}')
    assert 'In cart: eggs, milk' in client.wait_for_reply(without)

    saved = client.http.put(f'/api/skills/{draft["id"]}', json={**draft, 'draft': False})
    assert saved.status_code == 200, saved.text
    assert saved.json()['draft'] is False

    # Saved: Sammy loads the skill and does it in one go.
    shop.carts.clear()
    taught_run = client.ask(f'Reorder my last order at {shop.url}')
    assert 'In cart: eggs, milk' in client.wait_for_reply(taught_run)
    assert model_calls(database_url, taught_run) < model_calls(database_url, without)


@pytest.mark.scripted
def test_sammy_saves_a_skill_when_the_user_approves(app: App, client: Client) -> None:
    client.sign_up()
    thread = client.ask('Save a skill for checking my cart')
    approval = client.wait_for_ask(thread, 'approval')
    assert approval['prompt'].startswith('Save a skill, "Check my cart"')
    client.answer(approval, approved=True)
    assert 'Saved the skill' in client.wait_for_reply(thread)

    (skill,) = client.http.get('/api/skills').json()
    assert (skill['name'], skill['draft']) == ('Check my cart', False)

    # The apps edit it, refuse an empty one or a name in use, and delete it.
    edited = client.http.put(f'/api/skills/{skill["id"]}', json={**skill, 'name': 'Read my cart'})
    assert edited.status_code == 200 and edited.json()['name'] == 'Read my cart'
    assert client.http.put(f'/api/skills/{skill["id"]}', json={**skill, 'steps': ' '}).status_code == 422

    again = client.ask('Save a skill for checking my cart')
    client.answer(client.wait_for_ask(again, 'approval'), approved=True)
    client.wait_for_reply(again)
    names = sorted(s['name'] for s in client.http.get('/api/skills').json())
    assert names == ['Check my cart', 'Read my cart']
    clash = client.http.put(f'/api/skills/{skill["id"]}', json={**skill, 'name': 'check my cart'})
    assert clash.status_code == 409

    assert client.http.delete(f'/api/skills/{skill["id"]}').status_code == 200
    assert client.http.delete(f'/api/skills/{skill["id"]}').status_code == 404
    assert [s['name'] for s in client.http.get('/api/skills').json()] == ['Check my cart']
