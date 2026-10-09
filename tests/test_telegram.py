"""#140: the Telegram adapter's own parts (`sammy.channels.telegram`), against recorded updates and a fake Bot API:
the secret header, reading updates, MarkdownV2, and each Bot API call. The flows are in `e2e/test_telegram_bot.py`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fake_channel import Call, Reply, reply
from fake_telegram import TOKEN, TelegramAPI, update
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from sammy.channels import linking
from sammy.channels.base import Button, ButtonAnswer, Inbound, InboundFile, RawRequest, button_data
from sammy.channels.registry import Channels
from sammy.channels.telegram import SECRET_HEADER, TelegramChannel, TelegramError, new_channel
from sammy.channels.telegram_markdown import plain, to_markdown_v2
from sammy.settings import Settings

SECRET = 'telegram-webhook-secret_1'
PAT, GROUP = '7001001', '-1001234567890'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def api() -> Iterator[TelegramAPI]:
    server = TelegramAPI()
    yield server
    server.close()


@pytest.fixture
def telegram(api: TelegramAPI) -> TelegramChannel:
    return TelegramChannel(TOKEN, SECRET, api_url=api.url)


@pytest.fixture
async def open_telegram(telegram: TelegramChannel) -> AsyncIterator[TelegramChannel]:
    yield telegram
    await telegram.aclose()


def request(body: dict[str, object] | bytes, secret: str | None = SECRET) -> RawRequest:
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return RawRequest(method='POST', path='/api/channels/telegram/webhook', headers=headers, query={}, body=data)


# --- inbound ---


def test_only_a_request_with_the_webhook_secret_is_from_telegram(telegram: TelegramChannel) -> None:
    body = update('private_text')
    assert telegram.verify(request(body))
    assert not telegram.verify(request(body, secret='telegram-webhook-secret_2'))
    assert not telegram.verify(request(body, secret=''))
    assert not telegram.verify(request(body, secret=None))
    assert telegram.challenge(request(body)) is None  # no handshake: an unauthenticated request gets nothing early


def test_private_messages_and_start_codes_are_read(telegram: TelegramChannel, api: TelegramAPI) -> None:
    [start] = telegram.parse(request(update('private_start')))
    assert start == Inbound(delivery_id='512340001', sender_id=PAT, chat_id=PAT, direct=True, text='/start ABCDEFGHIJ')
    assert linking.code_in(start.text) == 'ABCDEFGHIJ'  # the shared layer links on it
    [text] = telegram.parse(request(update('private_text')))
    assert (text.text, text.direct, text.delivery_id) == ('Say hello', True, '512340002')
    assert telegram.parse(request(update('edited_message'))) == []
    with pytest.raises(ValueError):
        telegram.parse(request(b'{"not": "an update"}'))
    assert api.called('getMe') == []  # a direct chat never needs to know who the bot is


def test_in_a_group_only_messages_for_the_bot_count(telegram: TelegramChannel, api: TelegramAPI) -> None:
    quiet = update('group_chatter')
    message = quiet['message']
    assert isinstance(message, dict)
    message.pop('entities')
    assert telegram.parse(request(quiet)) == []
    assert api.called('getMe') == []  # nothing could be for the bot: Telegram is not asked who it is
    assert telegram.parse(request(update('group_chatter'))) == []  # someone else was mentioned
    [mention] = telegram.parse(request(update('group_mention')))
    assert (mention.text, mention.direct, mention.chat_id) == ('\N{WAVING HAND SIGN} Say hello', False, GROUP)
    [answer] = telegram.parse(request(update('group_reply')))
    assert (answer.text, answer.reply_to) == ('Blue', '403')
    assert len(api.called('getMe')) == 1  # once per process


def test_a_button_press_and_files_are_read(telegram: TelegramChannel) -> None:
    [pressed] = telegram.parse(request(update('callback_query')))
    ask_id = '0b4a0e1c-1f7e-5d0e-9f3a-0c4d2b9e7a11'
    assert pressed.answer == ButtonAnswer(ask_id=ask_id, choice='approve')
    assert (pressed.answer_message_id, pressed.chat_id, pressed.sender_id) == ('13', PAT, PAT)
    other = update('callback_query')
    query = other['callback_query']
    assert isinstance(query, dict)
    query['data'] = 'someone-elses-button'
    assert telegram.parse(request(other)) == []

    [document] = telegram.parse(request(update('document')))
    assert document.text == 'Describe what I attached'
    assert document.files == (
        InboundFile(id='BQACAgQAAxkBAAMOZ3notes', name='notes.md', media_type='text/markdown', size=23),
    )
    [photo] = telegram.parse(request(update('photo')))
    assert photo.files == (
        InboundFile(id='AgACAgQAAxkBAAMPlarge', name='photo-15.jpg', media_type='image/jpeg', size=48211),
    )


# --- MarkdownV2 ---


@pytest.mark.parametrize(
    ('markdown', 'expected'),
    [
        ('Hello! I am Sammy.', 'Hello\\! I am Sammy\\.'),
        ('Costs (about) 1+1=2 #tag {x} |y| ~z > w', 'Costs \\(about\\) 1\\+1\\=2 \\#tag \\{x\\} \\|y\\| \\~z \\> w'),
        ('**bold** and *it* and _it_ and ~~gone~~', '*bold* and _it_ and _it_ and ~gone~'),
        ('a `x_y*z\\` b`', 'a `x_y*z\\\\` b\\`'),
        ('```python\nprint("`hi`") # 1.\n```', '```python\nprint("\\`hi\\`") # 1.\n```'),
        ('[the docs](https://ex.com/a_(b)) now', '[the docs](https://ex.com/a_(b\\)) now'),
        ('see https://ex.com/a_b.html', 'see https://ex\\.com/a\\_b\\.html'),
        ('# Plan **A**\n- one\n* two\n> said.', '*Plan A*\n• one\n• two\n>said\\.'),
        ('snake_case and 2*3*4 and a * star', 'snake\\_case and 2\\*3\\*4 and a \\* star'),
    ],
)
def test_markdown_becomes_strict_markdown_v2(markdown: str, expected: str) -> None:
    assert to_markdown_v2(markdown) == expected


def test_plain_undoes_the_escapes() -> None:
    text = 'Hello! (1+1=2) snake_case [d] {e} 3.5 \\ end.'
    assert plain(to_markdown_v2(text)) == text
    assert plain('Hello\\! \\\\n \\#') == 'Hello! \\n #'


# --- outbound ---


@pytest.mark.anyio
async def test_messages_go_out_as_markdown_v2_with_an_inline_keyboard(
    open_telegram: TelegramChannel, api: TelegramAPI
) -> None:
    buttons = [
        Button(label='Approve', data=button_data('ask-1', 'approve')),
        Button(label='Decline', data='ask-1:decline'),
    ]
    sent = await open_telegram.send(PAT, open_telegram.format('Approve this?'), buttons, None)
    [call] = api.called('sendMessage')
    assert sent == str(api.message_id(call))
    assert call.json() == {
        'chat_id': PAT,
        'text': 'Approve this?',
        'parse_mode': 'MarkdownV2',
        'reply_markup': {
            'inline_keyboard': [
                [
                    {'text': 'Approve', 'callback_data': 'ask-1:approve'},
                    {'text': 'Decline', 'callback_data': 'ask-1:decline'},
                ]
            ]
        },
    }
    await open_telegram.send(PAT, 'Yes\\.', [], '12')
    assert api.called('sendMessage')[1].json()['reply_parameters'] == {
        'message_id': 12,
        'allow_sending_without_reply': True,
    }

    await open_telegram.edit(PAT, sent, open_telegram.format('Approve this?\n\nYou approved.'))
    [edit] = api.called('editMessageText')
    assert edit.json() == {
        'chat_id': PAT,
        'message_id': int(sent),
        'text': 'Approve this?\n\nYou approved\\.',
        'parse_mode': 'MarkdownV2',
    }  # no reply_markup: the buttons are gone


@pytest.mark.anyio
async def test_text_telegram_cannot_parse_goes_again_as_plain_text(
    open_telegram: TelegramChannel, api: TelegramAPI
) -> None:
    refused: Reply = reply(
        {'ok': False, 'error_code': 400, 'description': "Bad Request: can't parse entities: Can't find end of Bold"},
        400,
    )
    normal = api.routes[('POST', f'/bot{TOKEN}/sendMessage')]

    def picky(call: Call) -> Reply:
        return refused if 'parse_mode' in call.json() else normal(call)

    api.routes[('POST', f'/bot{TOKEN}/sendMessage')] = picky
    await open_telegram.send(PAT, '*cut in two\\!', [], None)
    first, second = api.called('sendMessage')
    assert first.json()['parse_mode'] == 'MarkdownV2'
    assert second.json() == {'chat_id': PAT, 'text': '*cut in two!'}

    api.routes[('POST', f'/bot{TOKEN}/sendMessage')] = lambda call: reply(
        {'ok': False, 'error_code': 403, 'description': 'Forbidden: bot was blocked by the user'}, 403
    )
    with pytest.raises(TelegramError) as raised:
        await open_telegram.send(PAT, 'hi', [], None)
    assert str(raised.value) == 'sendMessage: 403 Forbidden: bot was blocked by the user'
    assert TOKEN not in repr(raised.value) and raised.value.__cause__ is None


@pytest.mark.anyio
async def test_files_go_both_ways(open_telegram: TelegramChannel, api: TelegramAPI) -> None:
    png = b'\x89PNG\r\n\x1a\n' + b'0' * 64
    photo_id = await open_telegram.send_file(PAT, 'chart.png', 'image/png', png)
    [photo] = api.called('sendPhoto')
    assert photo_id == str(api.message_id(photo))
    assert b'name="photo"; filename="chart.png"' in photo.body and png in photo.body
    assert b'name="chat_id"' in photo.body and PAT.encode() in photo.body

    await open_telegram.send_file(PAT, 'report.csv', 'text/csv', b'item,total\neggs,3\n')
    await open_telegram.send_file(PAT, 'party.gif', 'image/gif', b'GIF89a')  # sendPhoto would drop the animation
    documents = api.called('sendDocument')
    assert [b'filename="report.csv"' in d.body for d in documents] == [True, False]
    assert b'item,total\neggs,3\n' in documents[0].body and b'filename="party.gif"' in documents[1].body

    api.serve_file('BQACAgQAAxkBAAMOZ3notes', b'# Groceries\neggs, milk\n')
    [message] = open_telegram.parse(request(update('document')))
    [document] = message.files
    assert await open_telegram.download(document) == b'# Groceries\neggs, milk\n'
    assert api.called('getFile')[0].json() == {'file_id': 'BQACAgQAAxkBAAMOZ3notes'}
    with pytest.raises(TelegramError, match='file: 404'):
        await open_telegram.download(InboundFile(id='gone', name='gone.txt'))


@pytest.mark.anyio
async def test_the_webhook_is_set_with_its_secret(open_telegram: TelegramChannel, api: TelegramAPI) -> None:
    await open_telegram.set_webhook('https://sammy.example/api/channels/telegram/webhook')
    assert api.called('setWebhook')[0].json() == {
        'url': 'https://sammy.example/api/channels/telegram/webhook',
        'secret_token': SECRET,
        'allowed_updates': ['message', 'callback_query'],
    }


@pytest.mark.anyio
async def test_bot_api_calls_are_not_traced(api: TelegramAPI) -> None:
    """The token is in every Bot API URL, so the adapter's calls make no HTTP spans, where other httpx calls do."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = HTTPXClientInstrumentor()
    already = instrumentor.is_instrumented_by_opentelemetry
    if already:
        instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    try:
        telegram = TelegramChannel(TOKEN, SECRET, api_url=api.url)
        try:
            await telegram.send(PAT, 'hi', [], None)
            await telegram.set_webhook('https://sammy.example/api/channels/telegram/webhook')
        finally:
            await telegram.aclose()
        async with httpx.AsyncClient() as client:
            await client.post(f'{api.url}/elsewhere')
    finally:
        instrumentor.uninstrument()
        if already:
            instrumentor.instrument()
    assert len(api.called('sendMessage')) == 1
    spans = exporter.get_finished_spans()
    assert [str((s.attributes or {}).get('url.full', (s.attributes or {}).get('http.url'))) for s in spans] == [
        f'{api.url}/elsewhere'
    ]


# --- registration ---


def settings(**values: str) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url='postgresql://unused',
        session_secret=SecretStr('s'),
        encryption_key=SecretStr('k'),
        **values,  # pyright: ignore[reportArgumentType]
    )


def test_telegram_is_on_only_with_its_token_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_WEBHOOK_SECRET', raising=False)
    assert new_channel(settings()) is None
    assert new_channel(settings(telegram_bot_token=TOKEN)) is None
    assert new_channel(settings(telegram_webhook_secret=SECRET)) is None
    assert new_channel(settings(telegram_bot_token='', telegram_webhook_secret=SECRET)) is None  # blank is unset
    both = settings(telegram_bot_token=TOKEN, telegram_webhook_secret=SECRET)
    assert isinstance(new_channel(both), TelegramChannel)
    assert Channels.from_settings(both).names == ['telegram']  # built in
    assert Channels.from_settings(settings()).names == []
    assert TOKEN not in repr(both)
