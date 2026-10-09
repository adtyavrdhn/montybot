"""#141: the WhatsApp adapter's own parts, offline: the verify handshake, signatures, recorded webhooks read into
messages, formatting, and its Cloud API calls against a fake (`tests/fake_whatsapp.py`). The flows are in
`e2e/test_whatsapp_chat.py`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from fake_whatsapp import (
    APP_SECRET,
    PAT,
    PHONE_NUMBER_ID,
    TOKEN,
    VERIFY_TOKEN,
    VERSION,
    CloudAPI,
    message_of,
    new_channel,
    sign,
    value_of,
    webhook,
)
from pydantic import SecretStr

from sammy.channels.base import Button, ButtonAnswer, Inbound, InboundFile, RawRequest, button_data
from sammy.channels.registry import Channels
from sammy.channels.whatsapp import SIGNATURE_HEADER, WhatsAppChannel, WhatsAppError
from sammy.channels.whatsapp import new_channel as real_channel
from sammy.channels.whatsapp_markdown import to_whatsapp
from sammy.settings import Settings

pytestmark = pytest.mark.anyio
ASK_ID = '0b4a0e1c-1f7e-5d0e-9f3a-0c4d2b9e7a11'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def api() -> Iterator[CloudAPI]:
    server = CloudAPI()
    yield server
    server.close()


@pytest.fixture
async def channel(api: CloudAPI, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WhatsAppChannel]:
    monkeypatch.setenv('FAKE_WHATSAPP_URL', api.api_url)
    made = new_channel(settings())
    assert made is not None
    yield made
    await made.aclose()


def settings(**values: str) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url='postgresql://unused',
        session_secret=SecretStr('unused'),
        encryption_key=SecretStr('unused'),
        **values,  # pyright: ignore[reportArgumentType]
    )


def post(body: bytes, headers: dict[str, str] | None = None) -> RawRequest:
    return RawRequest(method='POST', path='/api/channels/whatsapp/webhook', headers=headers or {}, query={}, body=body)


def get(query: dict[str, str]) -> RawRequest:
    return RawRequest(method='GET', path='/api/channels/whatsapp/webhook', headers={}, query=query, body=b'')


def parsed(channel: WhatsAppChannel, name: str) -> list[Inbound]:
    body = json.dumps(webhook(name)).encode()
    return channel.parse(post(body, {SIGNATURE_HEADER: sign(body)}))


# --- on only with all its credentials ---


def test_whatsapp_is_on_only_with_all_four_credentials() -> None:
    every = {
        'whatsapp_access_token': TOKEN,
        'whatsapp_phone_number_id': PHONE_NUMBER_ID,
        'whatsapp_app_secret': APP_SECRET,
        'whatsapp_verify_token': VERIFY_TOKEN,
    }
    for missing in every:
        assert real_channel(settings(**{k: v for k, v in every.items() if k != missing})) is None
        assert real_channel(settings(**(every | {missing: ''}))) is None  # blank, as in .env.example, is unset
    channels = Channels.from_settings(settings(**every))
    assert channels.names == ['whatsapp'] and isinstance(channels.get('whatsapp'), WhatsAppChannel)
    assert Channels.from_settings(settings()).names == []
    assert (settings().whatsapp_template, settings().whatsapp_template_language) == ('sammy_update', 'en')


# --- the handshake and signatures ---


async def test_the_verify_handshake_answers_only_with_the_token(channel: WhatsAppChannel) -> None:
    handshake = {'hub.mode': 'subscribe', 'hub.verify_token': VERIFY_TOKEN, 'hub.challenge': '1158201444'}
    answer = channel.challenge(get(handshake))
    assert answer is not None and answer.status_code == 200 and bytes(answer.body) == b'1158201444'
    assert answer.headers['content-type'].startswith('text/plain')
    for wrong in ({'hub.verify_token': 'guess'}, {'hub.verify_token': ''}, {'hub.mode': 'unsubscribe'}):
        assert channel.challenge(get(handshake | wrong)) is None  # on to `verify`, which refuses it
    assert channel.challenge(get({k: v for k, v in handshake.items() if k != 'hub.verify_token'})) is None
    assert channel.challenge(RawRequest(method='POST', path='/', headers={}, query=handshake, body=b'')) is None
    assert not channel.verify(get(handshake))


async def test_a_webhook_is_read_only_with_the_apps_signature(channel: WhatsAppChannel) -> None:
    body = json.dumps(webhook('text')).encode()
    assert channel.verify(post(body, {SIGNATURE_HEADER: sign(body)}))
    assert not channel.verify(post(body, {SIGNATURE_HEADER: sign(body, 'another-secret')}))
    assert not channel.verify(post(body, {SIGNATURE_HEADER: sign(body)[len('sha256=') :]}))
    assert not channel.verify(post(body + b' ', {SIGNATURE_HEADER: sign(body)}))
    assert not channel.verify(post(body, {}))


# --- recorded webhooks ---


async def test_recorded_webhooks_become_messages(channel: WhatsAppChannel) -> None:
    [text] = parsed(channel, 'text')
    assert text == Inbound(
        delivery_id='wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=',
        sender_id=PAT,
        chat_id=PAT,
        direct=True,
        text='Say hello',
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    [pressed] = parsed(channel, 'button_reply')
    assert pressed.answer == ButtonAnswer(ask_id=ASK_ID, choice='approve')
    assert pressed.answer_message_id == 'wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI1RjQyNUE3NEYxMzAzMzQ5MkEA'
    assert pressed.text == '' and pressed.reply_to is None

    [quick] = parsed(channel, 'quick_reply')  # the template's button: only opens the window
    assert (quick.text, quick.files, quick.answer) == ('', (), None)

    [image] = parsed(channel, 'image')
    assert image.text == 'Describe what I attached'
    assert image.files == (InboundFile(id='1003383421387256', name='image.jpg', media_type='image/jpeg'),)
    [document] = parsed(channel, 'document')
    assert document.files == (InboundFile(id='1185526832718395', name='notes.md', media_type='text/markdown'),)
    [voice] = parsed(channel, 'voice')
    assert voice.files[0].media_type == 'audio/ogg' and voice.files[0].name == 'audio.ogg'

    assert parsed(channel, 'statuses') == []  # delivery and read receipts
    assert parsed(channel, 'reaction') == []

    elsewhere = webhook('text')
    value_of(elsewhere)['metadata'] = {'display_phone_number': '15550001111', 'phone_number_id': '999'}
    assert channel.parse(post(json.dumps(elsewhere).encode())) == []  # another number on the same app
    foreign = webhook('button_reply')
    message_of(foreign)['interactive'] = {'type': 'button_reply', 'button_reply': {'id': 'other', 'title': 'Other'}}
    assert channel.parse(post(json.dumps(foreign).encode())) == []
    start = channel.parse(post(json.dumps(webhook('text', text='/start ABCDEFGHJK')).encode()))
    assert [m.text for m in start] == ['/start ABCDEFGHJK'] and start[0].direct
    with pytest.raises(ValueError):
        channel.parse(post(b'{"entry": "nope"}'))


def test_markdown_becomes_whatsapp_formatting() -> None:
    assert to_whatsapp('**Bold**, *italic*, __also bold__ and ~~gone~~') == '*Bold*, _italic_, *also bold* and ~gone~'
    assert to_whatsapp('# Groceries\n\n* eggs\n+ milk\n- tea') == '*Groceries*\n\n- eggs\n- milk\n- tea'
    assert to_whatsapp('See [the chat](https://x.test/#/t/1) or https://x.test') == (
        'See the chat (https://x.test/#/t/1) or https://x.test'
    )
    assert to_whatsapp('[https://x.test](https://x.test)') == 'https://x.test'
    assert to_whatsapp('Run `a **b**` then\n```python\nx = 2 * 3 * 4\n```') == (
        'Run `a **b**` then\n```\nx = 2 * 3 * 4\n```'
    )
    assert to_whatsapp('2 * 3 * 4 and snake_case_name') == '2 * 3 * 4 and snake_case_name'


# --- the Cloud API ---


async def test_messages_buttons_and_the_template_go_to_the_cloud_api(api: CloudAPI, channel: WhatsAppChannel) -> None:
    assert await channel.send(PAT, 'Hello!', (), None) == 'wamid.SAMMY0001'
    buttons = (Button(label='Approve', data=button_data(ASK_ID, 'approve')), Button(label='Decline', data='d'))
    await channel.send(PAT, 'Sammy needs your approval', buttons, 'wamid.IN')
    long = await channel.send(PAT, 'x' * 1500, buttons, None)
    await channel.reopen(PAT)
    text, ask, long_text, long_ask, template = api.messages()
    assert text == {
        'messaging_product': 'whatsapp',
        'recipient_type': 'individual',
        'to': PAT,
        'type': 'text',
        'text': {'preview_url': False, 'body': 'Hello!'},
    }
    assert ask['context'] == {'message_id': 'wamid.IN'} and ask['type'] == 'interactive'
    assert ask['interactive'] == {
        'type': 'button',
        'body': {'text': 'Sammy needs your approval'},
        'action': {
            'buttons': [
                {'type': 'reply', 'reply': {'id': f'{ASK_ID}:approve', 'title': 'Approve'}},
                {'type': 'reply', 'reply': {'id': 'd', 'title': 'Decline'}},
            ]
        },
    }
    # A body over WhatsApp's 1024 for buttons: the text, then the buttons on a short line, whose id is returned.
    assert long_text['text'] == {'preview_url': False, 'body': 'x' * 1500}
    assert long_ask['interactive']['body'] == {'text': 'Approve or decline?'}  # pyright: ignore[reportIndexIssue, reportArgumentType, reportCallIssue]
    assert long == 'wamid.SAMMY0004'
    assert template['type'] == 'template'
    assert template['template'] == {'name': 'sammy_update', 'language': {'code': 'en'}}
    for call in api.calls:
        assert call.headers['authorization'] == f'Bearer {TOKEN}'
    with pytest.raises(NotImplementedError):
        await channel.edit(PAT, 'wamid.SAMMY0001', 'edited')


async def test_files_are_uploaded_then_sent_and_downloaded_by_media_id(api: CloudAPI, channel: WhatsAppChannel) -> None:
    await channel.send_file(PAT, 'chart.png', 'image/png', b'\x89PNG...')
    await channel.send_file(PAT, 'report.csv', 'text/csv', b'item,total\neggs,3\n')
    first, second = api.calls_to(f'{VERSION}/{PHONE_NUMBER_ID}/media')
    assert b'name="messaging_product"\r\n\r\nwhatsapp' in first.body and b'filename="chart.png"' in first.body
    assert b'item,total\neggs,3\n' in second.body and b'name="type"\r\n\r\ntext/csv' in second.body
    image, document = api.messages()
    assert image['image'] == {'id': 'media0001'}
    assert document['document'] == {'id': 'media0003', 'filename': 'report.csv'}

    api.serve_media('1185526832718395', b'# Groceries\n', 'text/markdown')
    file = InboundFile(id='1185526832718395', name='notes.md', media_type='text/markdown')
    assert await channel.download(file) == b'# Groceries\n'
    lookup, fetched = api.calls[-2:]
    assert lookup.path == f'{VERSION}/1185526832718395' and fetched.path == '/media-files/1185526832718395'
    assert fetched.headers['authorization'] == f'Bearer {TOKEN}'  # Meta wants the token for the file too

    api.serve_media('huge', b'x' * (20 * 1024 * 1024 + 1), 'video/mp4')
    assert await channel.download(InboundFile(id='huge', name='video.mp4')) == b''  # too big: never fetched
    assert api.calls[-1].path == f'{VERSION}/huge'


async def test_a_refused_call_says_what_and_why_but_never_the_token(api: CloudAPI, channel: WhatsAppChannel) -> None:
    api.routes[('POST', f'{VERSION}/{PHONE_NUMBER_ID}/messages')] = lambda call: (
        400,
        json.dumps(
            {'error': {'message': 'Re-engagement message', 'type': 'OAuthException', 'code': 131047,
                       'fbtrace_id': 'Az8or2yhqkZfEZ-_4Qn_Bam'}}
        ).encode(),
        'application/json',
    )  # fmt: skip
    with pytest.raises(WhatsAppError) as raised:
        await channel.send(PAT, 'Hello!', (), None)
    assert raised.value.code == 131047 and raised.value.status == 400
    assert TOKEN not in str(raised.value) and 'Hello' not in str(raised.value)
    with pytest.raises(WhatsAppError):
        await channel.download(InboundFile(id='gone', name='x'))
