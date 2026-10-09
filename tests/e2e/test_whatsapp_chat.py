"""#141: talking to Sammy on WhatsApp, with recorded webhooks (`tests/fixtures/whatsapp/`) posted to the app and a fake
Cloud API (`tests/fake_whatsapp.py`): the verify handshake, signatures, linking with `/start <code>`, a reply, files
each way, an approval with reply buttons, and a scheduled result outside the 24-hour window, which goes as the
template first and in full once the user writes back. The shared flows are in `test_channels.py`."""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Iterator
from typing import LiteralString

import httpx
import psycopg
import pytest
from conftest import App, Client
from dbos import DBOSClient
from fake_whatsapp import (
    APP_SECRET,
    PHONE_NUMBER_ID,
    TEMPLATE,
    VERIFY_TOKEN,
    VERSION,
    CloudAPI,
    message_of,
    replies,
    signed_headers,
    webhook,
)
from helpers import eventually
from sites.slots import Slots

pytestmark = pytest.mark.scripted

WAIT = 180.0  # the CI machines and a loaded laptop are slow; tests wait for what the user would see
SLOT = 'Tuesday 18:00-19:00'
_ids = itertools.count(1)


@pytest.fixture
def api() -> Iterator[CloudAPI]:
    server = CloudAPI()
    yield server
    server.close()


@pytest.fixture
def app_env(api: CloudAPI) -> dict[str, str]:
    return {'CHANNEL_BACKENDS': 'fake_whatsapp:new_channel', 'FAKE_WHATSAPP_URL': api.api_url}


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


def post(app: App, body: dict[str, object], secret: str = APP_SECRET) -> httpx.Response:
    content = json.dumps(body).encode()
    return httpx.post(webhook_url(app), content=content, headers=signed_headers(content, secret), timeout=60)


def webhook_url(app: App) -> str:
    return f'{app.url}/api/channels/whatsapp/webhook'


def written_now(name: str, *, message_id: str | None = None, text: str | None = None) -> dict[str, object]:
    """The recorded message `name`, sent now (the reply window counts from its timestamp), as a new message unless
    given an id."""
    body = webhook(name, message_id=message_id or f'wamid.TEST{next(_ids):06d}', text=text)
    message_of(body)['timestamp'] = str(int(time.time()))
    return body


def say(app: App, name: str = 'text', text: str | None = None, *, message_id: str | None = None) -> None:
    """Pat sends the recorded message `name`, with `text` in place of its text."""
    assert post(app, written_now(name, message_id=message_id, text=text)).status_code == 200


def wait_for(api: CloudAPI, needle: str, *, after: int = 0) -> str:
    def found() -> str | None:
        return next((t for t in api.texts()[after:] if needle in t), None)

    return eventually(found, timeout=WAIT, what=f'a message with {needle!r}')


def count(database_url: str, query: LiteralString) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def test_pat_uses_sammy_from_whatsapp(
    app: App, client: Client, api: CloudAPI, slots: Slots, dbos: DBOSClient, database_url: str
) -> None:
    client.sign_up()

    # Meta's verify handshake is answered only with the verify token.
    hub = {'hub.mode': 'subscribe', 'hub.verify_token': VERIFY_TOKEN, 'hub.challenge': '1158201444'}
    answered = httpx.get(webhook_url(app), params=hub, timeout=60)
    assert answered.status_code == 200 and answered.text == '1158201444'
    assert httpx.get(webhook_url(app), params=hub | {'hub.verify_token': 'guess'}, timeout=60).status_code == 401

    # Without the app secret's signature, nothing is read or recorded; delivery statuses are taken and ignored.
    assert post(app, webhook('text'), secret='not-the-app-secret').status_code == 401
    assert httpx.post(webhook_url(app), content=json.dumps(webhook('text')), timeout=60).status_code == 401
    assert count(database_url, "SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE 'channel:%'") == 0
    assert post(app, webhook('statuses')).status_code == 200

    # A stranger gets only the way to link; the code from the web app, sent as /start, links them.
    say(app, text='Say hello')
    wait_for(api, '/#/link/')
    code = client.http.post('/api/channels/whatsapp/code', json={}).json()['code']
    say(app, text=f'/start {code}')
    wait_for(api, 'Linked. Say hi.')
    assert [i['channel'] for i in client.http.get('/api/channels').json()['linked']] == ['whatsapp']

    # A message and its reply; Meta delivering the same message twice is one message.
    for _ in range(2):
        say(app, text='Say hello', message_id='wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzRUI5MDhDQjc0RDY1QTkxQTJFRAA=')
    wait_for(api, 'Hello! I am Sammy.')
    assert api.texts().count('Hello! I am Sammy.') == 1

    # A file in, through its media id: the model sees it. A file out: uploaded, then sent as a document.
    api.serve_media('1185526832718395', b'# Groceries\neggs, milk\n', 'text/markdown')
    say(app, 'document')
    wait_for(api, 'file text: <file name="notes.md">\\n# Groceries\\neggs, milk')
    say(app, text='Make me a report')
    wait_for(api, 'Here is your report.')
    [report] = eventually(lambda: api.of_type('document') or None, timeout=WAIT, what='the report')
    assert report['filename'] == 'report.csv'
    [upload] = api.calls_to(f'{VERSION}/{PHONE_NUMBER_ID}/media')
    assert b'filename="report.csv"' in upload.body and b'item,total\neggs,3\n' in upload.body

    # An approval comes with reply buttons; pressing Approve sets the watch up.
    say(app, text=f'Tell me when a delivery slot opens at {slots.url}')
    [asked] = eventually(lambda: api.of_type('interactive') or None, timeout=WAIT, what='the reply buttons')
    approve, decline = replies(asked)
    assert (approve['title'], decline['title']) == ('Approve', 'Decline')
    press = written_now('button_reply')
    message_of(press)['interactive'] = {'type': 'button_reply', 'button_reply': approve}
    assert post(app, press).status_code == 200
    wait_for(api, 'Scheduled')
    (watch,) = client.http.get('/api/schedules').json()

    # A day later the 24-hour window has closed, and the watch finds a slot. WhatsApp allows only the approved template
    # now, so that goes first and the result waits for Pat.
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE sammy.channel_windows SET last_inbound_at = last_inbound_at - interval '25 hours', "
            "reopened_at = reopened_at - interval '25 hours'"
        )
    runs = count(database_url, 'SELECT count(*) FROM sammy.runs')
    before, texts_before = len(api.messages()), len(api.texts())
    slots.open_slot = SLOT
    dbos.trigger_schedule(f'sammy-schedule-{watch["id"]}')
    [template] = eventually(lambda: api.of_type('template') or None, timeout=WAIT, what='the template')
    assert template == {'name': TEMPLATE, 'language': {'code': 'en'}}
    eventually(
        lambda: count(database_url, 'SELECT count(*) FROM sammy.channel_outbox WHERE held_at IS NOT NULL') or None,
        timeout=WAIT,
        what='the result held',
    )
    assert [m['type'] for m in api.messages()[before:]] == ['template']

    # Pat presses the template's quick reply: the window opens and the result arrives, starting no run.
    say(app, 'quick_reply')
    ping = wait_for(api, 'Sammy found what you asked it to watch for.', after=texts_before)
    assert ping.endswith(f'/#/t/{watch["thread_id"]}') and SLOT not in ping
    assert api.messages()[before]['type'] == 'template'
    assert count(database_url, 'SELECT count(*) FROM sammy.runs') == runs + 1  # the scheduled run only
    assert count(database_url, 'SELECT count(*) FROM sammy.channel_outbox WHERE failed_at IS NOT NULL') == 0
