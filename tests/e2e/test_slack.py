"""#139: talking to Sammy in Slack, with recorded Events API and interactivity payloads (`tests/fixtures/slack/`)
signed and posted to the app's webhook, and a fake Web API (`tests/fake_slack.py`): signatures (good, bad, stale) and
the URL handshake, linking with `/start <code>` in a DM, a reply, an approval on Block Kit buttons, a file each way,
and a mention answered in its Slack thread. The shared flows are in `test_channels.py`."""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from conftest import App, Client
from fake_slack import SlackAPI, fields, form, payload, signed
from helpers import eventually

from sammy.channels.base import ButtonAnswer
from sammy.channels.slack_mrkdwn import to_mrkdwn, unescape

pytestmark = pytest.mark.scripted

DM, TEAM = 'D0PAT0001', 'C0TEAM0001'
WAIT = 180.0  # the CI machines and a loaded laptop are slow; tests wait for what the user would see
_events = itertools.count(1)


@pytest.fixture
def api() -> Iterator[SlackAPI]:
    server = SlackAPI()
    yield server
    server.close()


@pytest.fixture
def app_env(api: SlackAPI) -> dict[str, str]:
    return {'CHANNEL_BACKENDS': 'fake_slack:new_channel', 'FAKE_SLACK_URL': api.url}


def post(app: App, body: bytes, headers: dict[str, str], content_type: str = 'application/json') -> httpx.Response:
    return httpx.post(
        f'{app.url}/api/channels/slack/webhook',
        content=body,
        headers={**headers, 'Content-Type': content_type},
        timeout=60,
    )


def say(app: App, name: str, text: str | None = None, *, event_id: str | None = None) -> None:
    """Pat sends the recorded event `name` (a fresh `event_id` unless given), with `text` in place of its text."""
    body = payload(name)
    body['event_id'] = event_id or f'Ev0E2E{next(_events):05d}'
    event = body['event']
    assert isinstance(event, dict)
    if text is not None:
        event['text'] = text
    data = json.dumps(body).encode()
    assert post(app, data, signed(data)).status_code == 200


def wait_for(api: SlackAPI, channel: str, needle: str, *, after: int = 0) -> str:
    def found() -> str | None:
        return next((t for t in api.texts(channel)[after:] if needle in t), None)

    return eventually(found, timeout=WAIT, what=f'a message with {needle!r}')


def count_workflows(database_url: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE 'channel:slack:%'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_pat_uses_sammy_from_slack(app: App, client: Client, api: SlackAPI, database_url: str) -> None:
    client.sign_up()

    # Signatures: only a fresh one with the app's signing secret is from Slack; nothing else is read or recorded.
    body = json.dumps(payload('message_im')).encode()
    assert post(app, body, signed(body, secret='another-apps-secret')).status_code == 401
    assert post(app, body, signed(body, timestamp=int(time.time()) - 6 * 60)).status_code == 401
    assert post(app, body, {}).status_code == 401
    assert count_workflows(database_url) == 0
    handshake = json.dumps(payload('url_verification')).encode()
    assert post(app, handshake, {}).status_code == 401
    answered = post(app, handshake, signed(handshake))
    assert answered.status_code == 200 and answered.text == '3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P'

    # A stranger gets only the way to link; the code from the web app, sent as /start in the DM, links them.
    say(app, 'message_im')
    wait_for(api, DM, '/#/link/')
    code = client.http.post('/api/channels/slack/code', json={}).json()['code']
    say(app, 'message_im_start', f'/start {code}')
    wait_for(api, DM, 'Linked. Say hi.')
    assert [i['channel'] for i in client.http.get('/api/channels').json()['linked']] == ['slack']

    # A message and its reply; Slack retrying the event (the same event_id) is one message.
    for _ in range(2):
        say(app, 'message_im', 'Say hello', event_id='Ev0E2ERETRY')
    wait_for(api, DM, 'Hello! I am Sammy.')
    assert api.texts(DM).count('Hello! I am Sammy.') == 1
    thread_id = next(t['id'] for t in client.http.get('/api/threads').json() if t['title'] == 'Say hello')

    # An approval on Block Kit buttons, pressed: the ask's message is then updated, without its buttons.
    say(app, 'message_im', 'Every Monday at 9, fill my cart at <http://shop.test>')
    asked = eventually(
        lambda: next((m for m in api.messages(DM) if 'blocks' in m), None), timeout=WAIT, what='the buttons'
    )
    sent_blocks = asked['blocks']
    assert isinstance(sent_blocks, list)
    [approve, decline] = sent_blocks[1]['elements']
    assert (approve['text']['text'], decline['text']['text']) == ('Approve', 'Decline')
    pressed = ButtonAnswer.from_data(approve['value'])
    assert pressed is not None and pressed.choice == 'approve'
    message_ts = api.ts_of(next(c for c in api.called('chat.postMessage') if 'blocks' in c.json()))
    press = payload('block_actions')
    container, actions = press['container'], press['actions']
    assert isinstance(container, dict) and isinstance(actions, list) and isinstance(actions[0], dict)
    container['message_ts'] = message_ts
    actions[0]['value'] = approve['value']
    data = form(press)
    assert post(app, data, signed(data), 'application/x-www-form-urlencoded').status_code == 200
    [edited] = eventually(lambda: api.called('chat.update') or None, timeout=WAIT, what='the ask updated')
    update = edited.json()
    assert (update['channel'], update['ts']) == (DM, message_ts)
    assert str(update['text']).endswith('You approved.') and len(update['blocks']) == 1  # pyright: ignore[reportArgumentType]
    reply = client.wait_for_reply(thread_id)
    wait_for(api, DM, unescape(to_mrkdwn(reply)).splitlines()[0][:40])

    # A file in, through files.info and its private URL with the bot token: the model sees it.
    api.serve_file('F0NOTES0001', b'# Groceries\neggs, milk\n')
    say(app, 'message_im_file')
    wait_for(api, DM, 'file text: <file name="notes.md">\\n# Groceries\\neggs, milk')
    assert fields(api.called('files.info')[0]) == {'file': 'F0NOTES0001'}

    # A file out: uploaded to Slack, then shared in the DM.
    say(app, 'message_im', 'Make me a report')
    wait_for(api, DM, 'Here is your report.')
    [done] = eventually(lambda: api.called('files.completeUploadExternal') or None, timeout=WAIT, what='the report')
    [shared] = json.loads(fields(done)['files'])
    assert fields(done)['channel_id'] == DM and shared['title'] == 'report.csv'
    assert b'item,total\neggs,3\n' in api.uploads[shared['id']]

    # In a channel: a mention is answered in the message's own Slack thread.
    say(app, 'app_mention')
    eventually(
        lambda: next((m for m in api.messages(TEAM) if 'Hello! I am Sammy.' in str(m['text'])), None),
        timeout=WAIT,
        what='the answer in the thread',
    )
    assert {m.get('thread_ts') for m in api.messages(TEAM)} == {'1767225700.000600'}
