"""#139: the Slack adapter's own parts (`sammy.channels.slack`), against recorded payloads and a fake Web API: request
signatures, reading events and button presses, mrkdwn, and each Web API call. The flows are in `e2e/test_slack.py`."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator

import pytest
from fake_slack import SIGNING_SECRET, TOKEN, SlackAPI, fields, form, payload, signed
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from sammy.channels import linking
from sammy.channels.base import Button, ButtonAnswer, Inbound, InboundFile, RawRequest, button_data
from sammy.channels.registry import Channels
from sammy.channels.slack import SlackChannel, SlackError, new_channel, place
from sammy.channels.slack_mrkdwn import from_slack, to_mrkdwn
from sammy.observability import configure_observability
from sammy.settings import Settings

PAT, DM, TEAM = 'U0PAT0001', 'D0PAT0001', 'C0TEAM0001'
ASK = '0b4a0e1c-1f7e-5d0e-9f3a-0c4d2b9e7a11'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def api() -> Iterator[SlackAPI]:
    server = SlackAPI()
    yield server
    server.close()


@pytest.fixture
def slack(api: SlackAPI) -> SlackChannel:
    return SlackChannel(TOKEN, SIGNING_SECRET, api_url=f'{api.url}/api')


@pytest.fixture
async def open_slack(slack: SlackChannel) -> AsyncIterator[SlackChannel]:
    yield slack
    await slack.aclose()


def request(
    body: dict[str, object] | bytes, headers: dict[str, str] | None = None, *, pressed: bool = False
) -> RawRequest:
    """A webhook request as Slack sends it: signed now unless `headers` are given."""
    data = body if isinstance(body, bytes) else (form(body) if pressed else json.dumps(body).encode())
    content_type = 'application/x-www-form-urlencoded' if pressed else 'application/json'
    sent = {'content-type': content_type, **(signed(data) if headers is None else headers)}
    return RawRequest(method='POST', path='/api/channels/slack/webhook', headers=sent, query={}, body=data)


# --- signatures ---


def test_only_a_freshly_signed_request_is_from_slack(slack: SlackChannel) -> None:
    body = json.dumps(payload('message_im')).encode()
    assert slack.verify(request(body))
    assert slack.verify(request(body, signed(body, timestamp=int(time.time()) - 290)))  # Slack's clock may lag a bit
    assert not slack.verify(request(body, signed(body, secret='another-apps-secret')))
    assert not slack.verify(request(body, signed(body, timestamp=int(time.time()) - 301)))  # stale: a replay
    assert not slack.verify(request(body, signed(body, timestamp=int(time.time()) + 301)))
    assert not slack.verify(request(body + b' ', signed(body)))  # the body changed on the way
    assert not slack.verify(request(body, {}))
    assert not slack.verify(request(body, {**signed(body), 'x-slack-request-timestamp': '1e9'}))
    assert not slack.verify(request(body, {**signed(body), 'x-slack-signature': 'v0=\N{SNOWMAN}'}))


def test_the_url_handshake_is_answered_only_when_signed(slack: SlackChannel) -> None:
    handshake = payload('url_verification')
    answer = slack.challenge(request(handshake))
    assert answer is not None and answer.body == b'3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P'
    body = json.dumps(handshake).encode()
    assert slack.challenge(request(body, {})) is None  # unsigned: the webhook then refuses it (401)
    assert slack.challenge(request(body, signed(body, timestamp=int(time.time()) - 600))) is None
    assert slack.challenge(request(payload('message_im'))) is None  # any other request: no handshake


# --- inbound ---


def test_direct_messages_and_start_codes_are_read(slack: SlackChannel) -> None:
    [text] = slack.parse(request(payload('message_im')))
    assert text == Inbound(delivery_id='Ev0PAT00001', sender_id=PAT, chat_id=DM, direct=True, text='Say hello')
    [start] = slack.parse(request(payload('message_im_start')))
    assert (start.text, start.direct, start.chat_id) == ('/start ABCDEFGHIJ', True, DM)
    assert linking.code_in(start.text) == 'ABCDEFGHIJ'  # the shared layer links on it
    [shared] = slack.parse(request(payload('message_im_file')))
    assert shared.text == 'Describe what I attached'
    assert shared.files == (InboundFile(id='F0NOTES0001', name='notes.md', media_type='text/markdown', size=23),)


def test_edits_bots_and_other_events_are_not_for_sammy(slack: SlackChannel) -> None:
    assert slack.parse(request(payload('message_changed'))) == []
    assert slack.parse(request(payload('bot_message'))) == []  # Sammy's own messages come back as events too
    in_a_dm = payload('app_mention')
    event = in_a_dm['event']
    assert isinstance(event, dict)
    event['channel'] = DM
    assert slack.parse(request(in_a_dm)) == []  # a DM is read from its message event only, not twice
    channel_message = payload('message_im')
    event = channel_message['event']
    assert isinstance(event, dict)
    event['channel_type'] = 'channel'
    assert slack.parse(request(channel_message)) == []  # in a channel, only a mention
    with pytest.raises(ValueError):
        slack.parse(request(b'{"not": "an event"}'))
    with pytest.raises(ValueError):
        slack.parse(request(b'nothing=here', pressed=True))


def test_a_mention_is_answered_in_its_slack_thread(slack: SlackChannel) -> None:
    [top] = slack.parse(request(payload('app_mention')))
    assert top == Inbound(
        delivery_id='Ev0TEAM00001', sender_id=PAT, chat_id=f'{TEAM}:1767225700.000600', direct=False, text='Say hello'
    )  # a message in the channel starts a thread under itself
    [reply] = slack.parse(request(payload('app_mention_thread')))
    assert reply.chat_id == top.chat_id  # a reply in that thread is the same Sammy thread
    assert reply.text == 'what about the page (https://example.com/a?b=1&c=2) & <this> from @U0SAM0002?'
    assert place(reply.chat_id) == (TEAM, '1767225700.000600') and place(DM) == (DM, None)


def test_a_button_press_is_read(slack: SlackChannel) -> None:
    [pressed] = slack.parse(request(payload('block_actions'), pressed=True))
    assert pressed == Inbound(
        delivery_id=f'1767225960.123456:{PAT}',
        sender_id=PAT,
        chat_id=DM,
        direct=True,
        answer=ButtonAnswer(ask_id=ASK, choice='approve'),
        answer_message_id='1767225900.000900',
    )
    in_a_thread = payload('block_actions')
    in_a_thread['channel'] = {'id': TEAM, 'name': 'team'}
    message = in_a_thread['message']
    assert isinstance(message, dict)
    message['thread_ts'] = '1767225700.000600'
    [threaded] = slack.parse(request(in_a_thread, pressed=True))
    assert (threaded.chat_id, threaded.direct) == (f'{TEAM}:1767225700.000600', False)

    others = payload('block_actions')
    actions = others['actions']
    assert isinstance(actions, list) and isinstance(actions[0], dict)
    actions[0]['value'] = 'someone-elses-button'
    assert slack.parse(request(others, pressed=True)) == []
    assert slack.parse(request({**payload('block_actions'), 'type': 'view_submission'}, pressed=True)) == []


# --- mrkdwn ---


@pytest.mark.parametrize(
    ('markdown', 'expected'),
    [
        ('Hello! I am Sammy.', 'Hello! I am Sammy.'),
        ('Tom & Jerry <3 > 2', 'Tom &amp; Jerry &lt;3 &gt; 2'),
        ('**bold** and *it* and _it_ and ~~gone~~', '*bold* and _it_ and _it_ and ~gone~'),
        ('**bold _and it_**', '*bold _and it_*'),
        ('see [the docs](https://ex.com/a?b=1&c=2) now', 'see <https://ex.com/a?b=1&amp;c=2|the docs> now'),
        ('a `x<y> *z*` b', 'a `x&lt;y&gt; *z*` b'),
        ('```python\nif a < b:\n    print("**hi**")\n```', '```\nif a &lt; b:\n    print("**hi**")\n```'),
        ('# Plan **A**\n- one\n* two\n> said <so>.', '*Plan A*\n\N{BULLET} one\n\N{BULLET} two\n>said &lt;so&gt;.'),
        ('snake_case and 2*3*4 and a * star', 'snake_case and 2*3*4 and a * star'),
    ],
)
def test_markdown_becomes_mrkdwn(markdown: str, expected: str) -> None:
    assert to_mrkdwn(markdown) == expected


def test_slack_text_is_read_as_written() -> None:
    assert (
        from_slack('<@U0SAMMYBOT> fill my cart at <http://shop.test>', 'U0SAMMYBOT')
        == 'fill my cart at http://shop.test'
    )
    assert from_slack('<http://shop.test|shop.test> <mailto:a@b.test|a@b.test>', None) == 'http://shop.test a@b.test'
    assert from_slack('<!here> in <#C0TEAM0001|team>, 1 &lt; 2', None) == '@here in #team, 1 < 2'


# --- outbound ---


@pytest.mark.anyio
async def test_messages_go_to_the_dm_or_the_thread_with_buttons_as_blocks(
    open_slack: SlackChannel, api: SlackAPI
) -> None:
    buttons = [
        Button(label='Approve', data=button_data(ASK, 'approve')),
        Button(label='Decline', data=f'{ASK}:decline'),
    ]
    ts = await open_slack.send(DM, 'Approve *this*?', buttons, None)
    [call] = api.called('chat.postMessage')
    assert ts == '1767226001.000100'
    assert call.json() == {
        'channel': DM,
        'text': 'Approve *this*?',
        'blocks': [
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Approve *this*?'}},
            {
                'type': 'actions',
                'elements': [
                    {'type': 'button', 'action_id': 'sammy-0', 'text': {'type': 'plain_text', 'text': 'Approve'},
                     'value': f'{ASK}:approve', 'style': 'primary'},
                    {'type': 'button', 'action_id': 'sammy-1', 'text': {'type': 'plain_text', 'text': 'Decline'},
                     'value': f'{ASK}:decline', 'style': 'danger'},
                ],
            },
        ],
    }  # fmt: skip
    await open_slack.send(f'{TEAM}:1767225700.000600', 'In the thread.', [], None)
    assert api.called('chat.postMessage')[1].json() == {
        'channel': TEAM,
        'text': 'In the thread.',
        'thread_ts': '1767225700.000600',
    }

    await open_slack.edit(DM, ts, 'Approve *this*?\n\nYou approved.')
    [edit] = api.called('chat.update')
    text = 'Approve *this*?\n\nYou approved.'
    assert edit.json() == {
        'channel': DM,
        'ts': ts,
        'text': text,
        'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}],
    }  # no actions block: the buttons are gone
    await open_slack.edit(DM, ts, 'x' * 5000 + ' You approved.')
    shown = api.called('chat.update')[1].json()['text']
    assert isinstance(shown, str) and len(shown) == 3000 and shown.endswith('You approved.')


@pytest.mark.anyio
async def test_slack_errors_name_the_method_and_slacks_code_only(open_slack: SlackChannel, api: SlackAPI) -> None:
    api.routes[('POST', '/api/chat.postMessage')] = lambda call: (
        200,
        b'{"ok": false, "error": "channel_not_found"}',
        'application/json',
    )
    with pytest.raises(SlackError) as raised:
        await open_slack.send(DM, 'hi', [], None)
    assert str(raised.value) == 'chat.postMessage: channel_not_found'
    api.routes[('POST', '/api/chat.postMessage')] = lambda call: (429, b'', 'text/plain')
    with pytest.raises(SlackError, match='chat.postMessage: status 429'):
        await open_slack.send(DM, 'hi', [], None)
    wrong = SlackChannel('xoxb-wrong', SIGNING_SECRET, api_url=f'{api.url}/api')
    with pytest.raises(SlackError, match='invalid_auth') as raised:
        await wrong.edit(DM, '1767226001.000100', 'hi')
    await wrong.aclose()
    assert TOKEN not in repr(raised.value) and raised.value.__cause__ is None


@pytest.mark.anyio
async def test_files_go_both_ways(open_slack: SlackChannel, api: SlackAPI) -> None:
    file_id = await open_slack.send_file(f'{TEAM}:1767225700.000600', 'report.csv', 'text/csv', b'item,total\neggs,3\n')
    [ticket] = api.called('files.getUploadURLExternal')
    assert fields(ticket) == {'filename': 'report.csv', 'length': '18'}
    assert b'filename="report.csv"' in api.uploads[file_id] and b'item,total\neggs,3\n' in api.uploads[file_id]
    [done] = api.called('files.completeUploadExternal')
    assert fields(done) == {
        'files': json.dumps([{'id': file_id, 'title': 'report.csv'}]),
        'channel_id': TEAM,
        'thread_ts': '1767225700.000600',
    }
    await open_slack.send_file(DM, 'chart.png', 'image/png', b'\x89PNG')
    assert 'thread_ts' not in fields(api.called('files.completeUploadExternal')[1])

    api.serve_file('F0NOTES0001', b'# Groceries\neggs, milk\n')
    [message] = open_slack.parse(request(payload('message_im_file')))
    [notes] = message.files
    assert await open_slack.download(notes) == b'# Groceries\neggs, milk\n'
    assert fields(api.called('files.info')[0]) == {'file': 'F0NOTES0001'}
    with pytest.raises(SlackError, match='files.info: file_not_found'):
        await open_slack.download(InboundFile(id='F0GONE', name='gone.txt'))


@pytest.mark.anyio
async def test_the_token_goes_only_to_slack(open_slack: SlackChannel, api: SlackAPI) -> None:
    elsewhere = 'https://attacker.example/files-pri/x'
    api.routes[('POST', '/api/files.info')] = lambda call: (
        200,
        json.dumps({'ok': True, 'file': {'id': 'F1', 'url_private_download': elsewhere}}).encode(),
        'application/json',
    )
    with pytest.raises(SlackError, match='no private url on a Slack file host'):
        await open_slack.download(InboundFile(id='F1', name='x'))


@pytest.mark.anyio
async def test_file_transfers_make_no_http_spans(
    open_slack: SlackChannel, api: SlackAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Web API calls are spans like any other httpx call (the token is a header, never traced). A file's download and
    upload URLs hold its name or a one-time upload link, so those make only a `slack.download` / `slack.upload` span."""
    monkeypatch.delenv('LOGFIRE_TOKEN', raising=False)
    exporter = InMemorySpanExporter()
    configure_observability(settings(), span_processors=[SimpleSpanProcessor(exporter)])
    api.serve_file('F0NOTES0001', b'notes')
    await open_slack.send_file(DM, 'secret-plans.csv', 'text/csv', b'a,b\n')
    await open_slack.download(InboundFile(id='F0NOTES0001', name='notes.md'))
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans if span.name.startswith('POST')] == ['POST'] * 3  # the three API calls
    assert {'slack.upload', 'slack.download'} <= {span.name for span in spans}
    dumped = repr([(span.name, dict(span.attributes or {})) for span in spans])
    assert '/upload/' not in dumped and '/files-pri/' not in dumped and TOKEN not in dumped


# --- registration ---


def settings(**values: str) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url='postgresql://unused',
        session_secret=SecretStr('s'),
        encryption_key=SecretStr('k'),
        **values,  # pyright: ignore[reportArgumentType]
    )


def test_slack_is_on_only_with_its_token_and_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SLACK_BOT_TOKEN', raising=False)
    monkeypatch.delenv('SLACK_SIGNING_SECRET', raising=False)
    assert new_channel(settings()) is None
    assert new_channel(settings(slack_bot_token=TOKEN)) is None
    assert new_channel(settings(slack_signing_secret=SIGNING_SECRET)) is None
    assert new_channel(settings(slack_bot_token=' ', slack_signing_secret=SIGNING_SECRET)) is None  # blank is unset
    both = settings(slack_bot_token=TOKEN, slack_signing_secret=SIGNING_SECRET)
    assert isinstance(new_channel(both), SlackChannel)
    assert Channels.from_settings(both).names == ['slack']  # built in
    assert Channels.from_settings(settings()).names == []
    assert TOKEN not in repr(both) and SIGNING_SECRET not in repr(both)
