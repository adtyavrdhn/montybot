"""#138: people use Sammy from chat apps, through the shared chat layer (`sammy.channels`), here with a fake
platform (`tests/fake_channel.py`): linking, messages, asks, approvals, hand-offs, files both ways, pings, and a
restart in the middle of sending a long reply. An unlinked sender gets nothing but the way to link."""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Iterator
from typing import LiteralString

import httpx
import psycopg
import pytest
from conftest import App, Client
from fake_channel import SIGNATURE, Call, FakePlatform, sign
from helpers import eventually
from sites.shop import Shop

pytestmark = pytest.mark.scripted

SECRET = 'fake-channel-secret'
WAIT = 180.0  # the CI machines and a loaded laptop are slow; tests wait for what the user would see
LINK = re.compile(r'/#/link/([A-Z2-7]{10})$')


@pytest.fixture
def platform() -> Iterator[FakePlatform]:
    server = FakePlatform()
    yield server
    server.close()


@pytest.fixture
def app_env(platform: FakePlatform) -> dict[str, str]:
    return {
        'CHANNEL_BACKENDS': 'fake_channel:new_channel',
        'FAKE_CHANNEL_URL': platform.url,
        'FAKE_CHANNEL_SECRET': SECRET,
    }


class Person:
    """Someone in the fake chat app, writing in a direct chat with the bot (or in a group)."""

    def __init__(self, app: App, sender: str, *, chat: str | None = None, direct: bool = True) -> None:
        self.app = app
        self.sender = sender
        self.chat = chat or f'dm-{sender}'
        self.direct = direct

    def say(
        self,
        text: str = '',
        *,
        files: list[dict[str, object]] | None = None,
        button: str | None = None,
        delivery_id: str | None = None,
        secret: str = SECRET,
    ) -> httpx.Response:
        event = {
            'delivery_id': delivery_id or uuid.uuid4().hex,
            'sender_id': self.sender,
            'chat_id': self.chat,
            'direct': self.direct,
            'text': text,
            'files': files or [],
            'button': button,
        }
        body = json.dumps(event).encode()
        headers = {SIGNATURE: sign(body, secret), 'Content-Type': 'application/json'}
        return httpx.post(f'{self.app.url}/api/channels/fake/webhook', content=body, headers=headers, timeout=60)


def wait_for(platform: FakePlatform, chat: str, needle: str, *, after: int = 0) -> str:
    """The first message to `chat` (from the `after`-th on) that contains `needle`."""

    def found() -> str | None:
        return next((t for t in platform.texts(chat)[after:] if needle in t), None)

    return eventually(found, timeout=WAIT, what=f'a message with {needle!r}')


def link(client: Client, platform: FakePlatform, person: Person) -> None:
    """The unknown sender writes, gets a link, and opens it signed in."""
    assert person.say('hi').status_code == 200
    match = LINK.search(wait_for(platform, person.chat, '/#/link/'))
    assert match is not None
    assert client.http.post('/api/channels/link', json={'code': match.group(1)}).status_code == 200
    wait_for(platform, person.chat, 'Linked. Say hi.')
    assert client.http.post('/api/channels/link', json={'code': match.group(1)}).status_code == 404  # used up


def thread_of(client: Client, title: str) -> str:
    return next(t['id'] for t in client.http.get('/api/threads').json() if t['title'] == title)


def count(database_url: str, query: LiteralString) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def test_a_linked_chat_does_what_the_web_app_does(
    app: App, client: Client, platform: FakePlatform, database_url: str
) -> None:
    client.sign_up()
    pat = Person(app, 'pat')
    link(client, platform, pat)

    # A message, and Sammy's reply.
    assert pat.say('Say hello').status_code == 200
    wait_for(platform, pat.chat, 'Hello! I am Sammy.')
    thread_id = thread_of(client, 'Say hello')  # the chat is a thread on the web too

    # A question, answered in the chat.
    pat.say('Ask me my favourite colour')
    wait_for(platform, pat.chat, 'What is your favourite colour?\n\nReply with your answer.')
    pat.say('Blue')
    wait_for(platform, pat.chat, 'Got it: your favourite colour is Blue.')

    # An approval, answered with its button; the message then says what was chosen.
    pat.say('Every Monday at 9, fill my cart at http://shop.test')
    asked = eventually(
        lambda: next((m for m in platform.sent(pat.chat) if m.get('buttons')), None), timeout=WAIT, what='buttons'
    )
    buttons = asked['buttons']
    assert isinstance(buttons, list) and [b['label'] for b in buttons] == ['Approve', 'Decline']  # pyright: ignore[reportUnknownVariableType]
    assert pat.say(button=str(buttons[0]['data'])).status_code == 200  # pyright: ignore[reportUnknownArgumentType]
    edited = eventually(lambda: platform.edits() or None, timeout=WAIT, what='the ask edited')
    assert str(edited[0]['text']).endswith('You approved.')
    reply = client.wait_for_reply(thread_id)
    wait_for(platform, pat.chat, reply[:60])

    # A hand-off links to the chat on the web, where the take-over button is. Writing meanwhile sends it again.
    shop = Shop()
    shop.start()
    try:
        pat.say(f'Order eggs from {shop.url}')
        handoff = wait_for(platform, pat.chat, 'Sammy needs you to take over the browser')
        assert handoff.endswith(f'/#/t/{thread_id}') and '/live/' not in handoff
        seen = len(platform.texts(pat.chat))
        pat.say('Say hello')
        assert wait_for(platform, pat.chat, 'take over the browser', after=seen) == handoff
        run_id = client.thread(thread_id)['run']['id']
        assert client.http.post(f'/api/runs/{run_id}/stop', json={}).status_code == 200
        wait_for(platform, pat.chat, 'You stopped this.')
    finally:
        shop.stop()

    # A file in: the model sees it.
    platform.serve_file('f1', b'# Groceries\neggs, milk\n')
    notes = {'id': 'f1', 'name': 'notes.md', 'media_type': 'text/markdown', 'size': 23}
    pat.say('Describe what I attached', files=[notes])
    wait_for(platform, pat.chat, 'file text: <file name="notes.md">\\n# Groceries\\neggs, milk')

    # A file out: what Sammy shares comes to the chat.
    pat.say('Make me a report')
    wait_for(platform, pat.chat, 'Here is your report.')
    report = eventually(
        lambda: next((m for m in platform.sent(pat.chat) if m.get('name') == 'report.csv'), None),
        timeout=WAIT,
        what='the report',
    )
    assert base64.b64decode(str(report['data'])) == b'item,total\neggs,3\n'

    # A run started on the web pings the linked chat, saying no more than a web push does.
    web_thread = client.ask('Ask me my favourite colour')
    ping = wait_for(platform, pat.chat, 'Sammy has a question for you.')
    assert ping.endswith(f'/#/t/{web_thread}') and 'colour' not in ping
    client.answer(client.wait_for_ask(web_thread, 'question'), text='Green')
    client.wait_for_reply(web_thread)

    # The user's chat apps on the web: pings off, then unlinked; the sender is a stranger again.
    listed = client.http.get('/api/channels').json()
    assert listed['enabled'] == ['fake']
    assert [(i['channel'], i['notify']) for i in listed['linked']] == [('fake', True)]
    assert client.http.put('/api/channels/fake', json={'notify': False}).json() == {'ok': True}
    assert client.http.get('/api/channels').json()['linked'][0]['notify'] is False
    assert client.http.delete('/api/channels/fake').status_code == 200
    seen = len(platform.texts(pat.chat))
    pat.say('Say hello')
    wait_for(platform, pat.chat, '/#/link/', after=seen)
    assert count(database_url, 'SELECT count(*) FROM sammy.channel_outbox WHERE failed_at IS NOT NULL') == 0


def test_an_unlinked_sender_learns_nothing_and_starts_nothing(
    app: App, client: Client, platform: FakePlatform, database_url: str
) -> None:
    alice = client.sign_up()

    # Not from the platform: refused before anything is read, and nothing is recorded.
    stranger = Person(app, 'stranger-1')
    assert stranger.say('Say hello', secret='wrong').status_code == 401
    big = httpx.post(f'{app.url}/api/channels/fake/webhook', content=b'x' * (1024 * 1024 + 1), timeout=60)
    assert big.status_code == 413
    assert httpx.post(f'{app.url}/api/channels/nope/webhook', content=b'{}', timeout=60).status_code == 404
    assert count(database_url, "SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE 'channel:%'") == 0

    # In a group: only where to link, the same bytes for anyone.
    group = [Person(app, f'stranger-{n}', chat='group-1', direct=False) for n in (1, 2)]
    for person in group:
        assert person.say('Say hello').status_code == 200
    eventually(lambda: len(platform.texts('group-1')) == 2 or None, timeout=WAIT, what='two answers')
    assert platform.texts('group-1') == ['Message me directly to link your account.'] * 2

    # Directly: each gets their own link code and otherwise the same words; writing again gives the same code.
    one, two = Person(app, 'stranger-1'), Person(app, 'stranger-2')
    one.say('Say hello')
    two.say(f'Tell me about {alice["email"]}')
    first, second = wait_for(platform, one.chat, '/#/link/'), wait_for(platform, two.chat, '/#/link/')
    assert (
        LINK.sub('/#/link/CODE', first)
        == LINK.sub('/#/link/CODE', second)
        == (f'To use Sammy here, link your account: {app.url}/#/link/CODE')
    )
    assert first != second
    one.say('Ask me my favourite colour')
    eventually(lambda: len(platform.texts(one.chat)) == 2 or None, timeout=WAIT, what='a second answer')
    assert platform.texts(one.chat) == [first, first]
    assert count(database_url, 'SELECT count(*) FROM sammy.runs') == 0
    assert count(database_url, 'SELECT count(*) FROM sammy.threads') == 0
    assert client.http.post('/api/channels/link', json={'code': 'AAAAAAAAAA'}).status_code == 404

    # The other way round: a code from the web app, sent to the bot, links the sender.
    code = client.http.post('/api/channels/fake/code', json={}).json()['code']
    three = Person(app, 'stranger-3')
    three.say(f'/start {code}')
    wait_for(platform, three.chat, 'Linked. Say hi.')
    assert client.http.get('/api/channels').json()['linked'][0]['channel'] == 'fake'

    # The same delivery twice is one message.
    for _ in range(2):
        assert three.say('Say hello', delivery_id='delivery-1').status_code == 200
    wait_for(platform, three.chat, 'Hello! I am Sammy.')
    assert count(database_url, 'SELECT count(*) FROM sammy.runs') == 1
    assert platform.texts(three.chat).count('Hello! I am Sammy.') == 1


def test_a_restart_while_sending_a_long_reply_sends_each_part_once(
    app: App, client: Client, platform: FakePlatform
) -> None:
    client.sign_up()
    pat = Person(app, 'pat')
    link(client, platform, pat)

    def second_part(call: Call) -> bool:
        return call.path == '/send' and 'Part two' in str(call.json().get('text'))

    hold = platform.hold(second_part)
    pat.say('Tell me a long story')
    assert hold.held.wait(timeout=WAIT), 'the second part was never sent'
    app.kill()  # the platform never accepted the second part
    hold.released.set()
    app.start()

    wait_for(platform, pat.chat, 'Part three')
    parts = [t for t in platform.texts(pat.chat) if t.startswith('Part ')]
    assert [p.split(':')[0] for p in parts] == ['Part one', 'Part two', 'Part three']
    assert all(len(p) <= 200 for p in parts)
