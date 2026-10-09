"""#140: talking to Sammy through a Telegram bot, with recorded updates (`tests/fixtures/telegram/`) posted to the
app's webhook and a fake Bot API (`tests/fake_telegram.py`): the secret header, linking with `/start <code>`, a reply,
an approval on an inline keyboard, and a file each way. The shared flows are in `test_channels.py`."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from conftest import App, Client
from fake_telegram import TelegramAPI, update
from helpers import eventually

from sammy.channels.base import ButtonAnswer
from sammy.channels.telegram import SECRET_HEADER
from sammy.channels.telegram_markdown import plain, to_markdown_v2

pytestmark = pytest.mark.scripted

SECRET = 'telegram-webhook-secret_1'
PAT = 7001001
WAIT = 180.0  # the CI machines and a loaded laptop are slow; tests wait for what the user would see
_update_ids = itertools.count(612340001)


@pytest.fixture
def api() -> Iterator[TelegramAPI]:
    server = TelegramAPI()
    yield server
    server.close()


@pytest.fixture
def app_env(api: TelegramAPI) -> dict[str, str]:
    return {
        'CHANNEL_BACKENDS': 'fake_telegram:new_channel',
        'FAKE_TELEGRAM_URL': api.url,
        'FAKE_TELEGRAM_SECRET': SECRET,
    }


def post(app: App, body: dict[str, object], secret: str = SECRET) -> httpx.Response:
    headers = {SECRET_HEADER: secret, 'Content-Type': 'application/json'}
    content = json.dumps(body).encode()
    return httpx.post(f'{app.url}/api/channels/telegram/webhook', content=content, headers=headers, timeout=60)


def say(app: App, name: str, text: str | None = None, *, update_id: int | None = None) -> None:
    """Pat sends the recorded update `name`, with `text` in place of its text."""
    body = update(name, update_id or next(_update_ids))
    message = body['message']
    assert isinstance(message, dict)
    if text is not None:
        message['text'] = text
    assert post(app, body).status_code == 200


def wait_for(api: TelegramAPI, needle: str, *, after: int = 0) -> str:
    def found() -> str | None:
        return next((t for t in api.texts(PAT)[after:] if needle in t), None)

    return eventually(found, timeout=WAIT, what=f'a message with {needle!r}')


def count_workflows(database_url: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE 'channel:telegram:%'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_pat_uses_sammy_from_telegram(app: App, client: Client, api: TelegramAPI, database_url: str) -> None:
    client.sign_up()

    # Without the webhook's secret, nothing is read or recorded.
    assert post(app, update('private_text'), secret='wrong').status_code == 401
    assert post(app, update('private_text'), secret='').status_code == 401
    assert count_workflows(database_url) == 0

    # A stranger gets only the way to link; the code from the web app, sent as /start, links them.
    say(app, 'private_text')
    wait_for(api, '/#/link/')
    code = client.http.post('/api/channels/telegram/code', json={}).json()['code']
    say(app, 'private_start', f'/start {code}')
    wait_for(api, 'Linked. Say hi.')
    assert [i['channel'] for i in client.http.get('/api/channels').json()['linked']] == ['telegram']

    # A message and its reply, in MarkdownV2; Telegram delivering the update twice is one message.
    for _ in range(2):
        say(app, 'private_text', 'Say hello', update_id=612349999)
    wait_for(api, 'Hello! I am Sammy.')
    hello = next(m for m in api.messages(PAT) if 'Hello' in str(m['text']))
    assert hello['text'] == 'Hello\\! I am Sammy\\.' and hello['parse_mode'] == 'MarkdownV2'
    thread_id = next(t['id'] for t in client.http.get('/api/threads').json() if t['title'] == 'Say hello')

    # An approval on an inline keyboard, pressed: the ask's message then says so, without its buttons.
    say(app, 'private_text', 'Every Monday at 9, fill my cart at http://shop.test')
    asked = eventually(
        lambda: next((c for c in api.called('sendMessage') if 'reply_markup' in c.json()), None),
        timeout=WAIT,
        what='the inline keyboard',
    )
    markup = asked.json()['reply_markup']
    assert isinstance(markup, dict)
    [[approve, decline]] = markup['inline_keyboard']
    assert (approve['text'], decline['text']) == ('Approve', 'Decline')
    pressed = ButtonAnswer.from_data(approve['callback_data'])
    assert pressed is not None and pressed.choice == 'approve'
    press = update('callback_query', next(_update_ids))
    query = press['callback_query']
    assert isinstance(query, dict) and isinstance(query['message'], dict)
    query['data'] = approve['callback_data']
    query['message']['message_id'] = api.message_id(asked)
    assert post(app, press).status_code == 200
    edited = eventually(lambda: api.called('editMessageText') or None, timeout=WAIT, what='the ask edited')
    assert edited[0].json()['message_id'] == api.message_id(asked) and 'reply_markup' not in edited[0].json()
    assert str(edited[0].json()['text']).endswith('You approved\\.')
    reply = client.wait_for_reply(thread_id)
    wait_for(api, plain(to_markdown_v2(reply)).splitlines()[0][:40])

    # A file in, through getFile: the model sees it.
    api.serve_file('BQACAgQAAxkBAAMOZ3notes', b'# Groceries\neggs, milk\n')
    say(app, 'document')
    wait_for(api, 'file text: <file name="notes.md">\\n# Groceries\\neggs, milk')
    assert api.called('getFile')[0].json() == {'file_id': 'BQACAgQAAxkBAAMOZ3notes'}

    # A file out: what Sammy shares is sent as a document.
    say(app, 'private_text', 'Make me a report')
    wait_for(api, 'Here is your report.')
    [document] = eventually(lambda: api.called('sendDocument') or None, timeout=WAIT, what='the report')
    assert b'filename="report.csv"' in document.body and b'item,total\neggs,3\n' in document.body
    assert f'name="chat_id"\r\n\r\n{PAT}\r\n'.encode() in document.body
