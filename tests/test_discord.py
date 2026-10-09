"""#142: the Discord adapter (`sammy.channels.discord`) offline: reading recorded events, the HTTP calls, the gateway
protocol against `FakeGateway` (Identify, heartbeats, a drop and Resume, Reconnect, Invalid Session, a zombie
connection, a refused bot), and the leader lock that keeps the gateway on one replica. The whole app over the fake
gateway is `e2e/test_discord_bot.py`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator

import pytest
from fake_channel import reply
from fake_discord import (
    BOT_ID,
    CHANNEL_ID,
    DM_ID,
    PAT_ID,
    TOKEN,
    DiscordAPI,
    FakeGateway,
    click,
    message,
    payload,
)
from pydantic import SecretStr, ValidationError

from sammy.channels.base import Button, ButtonAnswer, Inbound, InboundFile, RawRequest, button_data
from sammy.channels.discord import DiscordChannel, DiscordError, thread_name
from sammy.channels.discord_gateway import INTENTS
from sammy.channels.leader import lead
from sammy.channels.registry import Channels
from sammy.settings import Settings

pytestmark = pytest.mark.anyio
ASK = '0b4a0e1c-1f7e-5d0e-9f3a-0c4d2b9e7a11'
OWN_THREAD, OTHER_THREAD = '1700000000000000011', '1700000000000000012'  # in guild_create.json


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    made = FakeGateway()
    yield made
    made.close()


@pytest.fixture
def api(gateway: FakeGateway) -> Iterator[DiscordAPI]:
    made = DiscordAPI(gateway)
    yield made
    made.close()


@pytest.fixture
async def channel(api: DiscordAPI) -> AsyncIterator[DiscordChannel]:
    made = DiscordChannel(TOKEN, api_url=api.url, files_url=api.url, backoff=0.05)
    yield made
    await made.aclose()


async def until(check: Callable[[], object], what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        await asyncio.sleep(0.02)


class Inbox:
    """Where the adapter delivers; `failures` deliveries fail first, as if the database were away."""

    def __init__(self) -> None:
        self.got: list[Inbound] = []
        self.failures = 0

    async def deliver(self, inbound: Inbound) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError('the database is away')
        self.got.append(inbound)


@contextlib.asynccontextmanager
async def listening(channel: DiscordChannel, inbox: Inbox) -> AsyncIterator[None]:
    task = asyncio.create_task(channel.listen(inbox.deliver))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def ready(channel: DiscordChannel) -> None:
    """What the gateway's first events tell the adapter: who the bot is, and which threads it made."""
    inbox = Inbox()
    await channel.handle('READY', payload('ready'), inbox.deliver)
    await channel.handle('GUILD_CREATE', payload('guild_create'), inbox.deliver)


# --- reading events ---


async def test_direct_messages_and_server_mentions_are_for_sammy_and_chatter_is_not(
    channel: DiscordChannel, api: DiscordAPI
) -> None:
    await ready(channel)

    said = message('Say hello')
    direct = await channel.message_of(said)
    assert direct == Inbound(
        delivery_id=str(said['id']), sender_id=PAT_ID, chat_id=DM_ID, direct=True, text='Say hello'
    )
    assert await channel.message_of(payload('server_chatter')) is None
    assert await channel.message_of(message('mine', author={'id': BOT_ID, 'bot': True})) is None
    assert await channel.message_of(message('pinned', type=6)) is None

    # A mention starts a thread made from the message; its id is the message's, and it is the chat.
    mention = payload('server_mention')
    first = await channel.message_of(mention)
    assert first is not None and first.chat_id == mention['id'] and not first.direct and first.text == 'Say hello'
    made = api.called('POST', f'/channels/{CHANNEL_ID}/messages/{mention["id"]}/threads')
    assert [c.json() for c in made] == [{'name': 'Say hello', 'auto_archive_duration': 1440}]
    # The same message again (a replay after a resume): the thread is there already, and the chat is the same.
    assert await channel.message_of(mention) == first

    # In that thread, and in one the bot made before (from GUILD_CREATE), no mention is needed; elsewhere it is.
    in_thread = await channel.message_of(message('And more', name='server_chatter', channel_id=first.chat_id))
    assert in_thread is not None and in_thread.chat_id == first.chat_id
    assert await channel.message_of(message('Hi', name='server_chatter', channel_id=OWN_THREAD)) is not None
    assert await channel.message_of(message('Hi', name='server_chatter', channel_id=OTHER_THREAD)) is None


async def test_a_mention_where_no_thread_can_be_made_is_answered_in_place() -> None:
    api_down = DiscordChannel(TOKEN, api_url='http://127.0.0.1:9', files_url='http://127.0.0.1:9')
    try:
        await ready(api_down)
        found = await api_down.message_of(payload('server_mention'))
        assert found is not None and found.chat_id == CHANNEL_ID
    finally:
        await api_down.aclose()


async def test_attachments_come_in_by_their_url_from_discords_file_host_only(
    channel: DiscordChannel, api: DiscordAPI
) -> None:
    await ready(channel)
    recorded = payload('attachment_message')
    attachments = recorded['attachments']
    assert isinstance(attachments, list)
    path = '/attachments/1300000000000000003/1460000000000000901/notes.md'
    attachments[0]['url'] = f'{api.url}{path}?ex=6789abcd&hm=0f1e'
    found = await channel.message_of(recorded)
    assert found is not None and len(found.files) == 1
    file = found.files[0]
    assert (file.name, file.media_type, file.size) == ('notes.md', 'text/markdown; charset=utf-8', 23)
    api.serve_file(path, b'# Groceries\neggs, milk\n')
    assert await channel.download(file) == b'# Groceries\neggs, milk\n'
    assert 'authorization' not in api.called('GET', path)[0].headers  # the token stays with the API
    with pytest.raises(DiscordError):
        await channel.download(InboundFile(id='http://169.254.169.254/latest/meta-data', name='x'))


async def test_a_click_is_answered_at_once_and_counts_as_an_answer(channel: DiscordChannel, api: DiscordAPI) -> None:
    await ready(channel)
    clicked = click(button_data(ASK, 'decline'), '1600000000000000042')
    found = await channel.interaction_of(clicked)
    assert found == Inbound(
        delivery_id=str(clicked['id']),
        sender_id=PAT_ID,
        chat_id=DM_ID,
        direct=True,
        answer=ButtonAnswer(ask_id=ASK, choice='decline'),
        answer_message_id='1600000000000000042',
    )
    answered = api.called('POST', f'/interactions/{clicked["id"]}/')
    assert [c.json() for c in answered] == [{'type': 6}]  # a deferred update: the edit comes once the ask is answered
    assert await channel.interaction_of(click('something:else', '1600000000000000042')) is None
    with pytest.raises(ValidationError):
        await channel.handle('MESSAGE_CREATE', {'id': 'no author'}, Inbox().deliver)


# --- sending ---


async def test_messages_buttons_edits_and_files_go_out_as_discord_takes_them(
    channel: DiscordChannel, api: DiscordAPI
) -> None:
    buttons = [Button(label='Approve', data=button_data(ASK, 'approve')), Button(label='Decline', data='x')]
    message_id = await channel.send(DM_ID, 'Sammy needs your approval', buttons, None)
    sent = api.messages(DM_ID)[0]
    assert api.message_id(sent) == message_id
    assert sent.headers['authorization'] == f'Bot {TOKEN}'
    assert sent.json() == {
        'content': 'Sammy needs your approval',
        'allowed_mentions': {'parse': []},
        'components': [
            {
                'type': 1,
                'components': [
                    {'type': 2, 'style': 3, 'label': 'Approve', 'custom_id': button_data(ASK, 'approve')},
                    {'type': 2, 'style': 2, 'label': 'Decline', 'custom_id': 'x'},
                ],
            }
        ],
    }
    await channel.edit(DM_ID, message_id, 'You approved.')
    edits = api.called('PATCH', f'/channels/{DM_ID}/messages/{message_id}')
    assert edits[0].json() == {'content': 'You approved.', 'components': [], 'allowed_mentions': {'parse': []}}

    await channel.send_file(DM_ID, 'report.csv', 'text/csv', b'item,total\neggs,3\n')
    upload = api.messages(DM_ID)[1]
    assert upload.headers['content-type'].startswith('multipart/form-data')
    assert b'filename="report.csv"' in upload.body and b'item,total\neggs,3\n' in upload.body
    assert b'"attachments": [{"id": 0, "filename": "report.csv"}]' in upload.body
    assert channel.capabilities.max_text == 2000 and channel.capabilities.max_file_bytes == 10 * 1024 * 1024

    api.routes[('POST', '/channels/42/messages')] = lambda call: reply(
        {'message': 'Missing Access', 'code': 50001}, 403
    )
    with pytest.raises(DiscordError) as refused:
        await channel.send('42', 'nowhere', (), None)
    assert refused.value.code == 50001 and TOKEN not in str(refused.value) and 'http' not in str(refused.value)
    assert thread_name('  \n') == 'Sammy' and len(thread_name('x' * 300)) == 100


async def test_the_webhook_route_takes_nothing_and_the_platform_is_on_only_with_its_token(
    channel: DiscordChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(payload('direct_message')).encode()
    raw = RawRequest(method='POST', path='/api/channels/discord/webhook', headers={}, query={}, body=body)
    assert channel.challenge(raw) is None and not channel.verify(raw) and channel.parse(raw) == []

    def settings(token: str | None) -> Settings:
        monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
        if token is not None:
            monkeypatch.setenv('DISCORD_BOT_TOKEN', token)
        return Settings(_env_file=None, database_url='x', session_secret=SecretStr('s'), encryption_key=SecretStr('k'))  # pyright: ignore[reportCallIssue]

    assert Channels.from_settings(settings(None)).names == []
    assert Channels.from_settings(settings('')).names == []
    on = Channels.from_settings(settings(TOKEN))
    assert on.names == ['discord'] and isinstance(on.get('discord'), DiscordChannel)
    await on.aclose()


# --- the gateway ---


async def test_the_gateway_identifies_resumes_after_a_drop_and_loses_nothing(
    channel: DiscordChannel, gateway: FakeGateway
) -> None:
    inbox = Inbox()
    async with listening(channel, inbox):
        await until(lambda: gateway.identifies, 'Identify')
        sent = gateway.identifies[0]
        assert sent['token'] == TOKEN and sent['intents'] == INTENTS == 1 | 1 << 9 | 1 << 12 | 1 << 15

        first = message('Say hello')
        gateway.dispatch('MESSAGE_CREATE', first)
        await until(lambda: inbox.got, 'the first message')
        assert inbox.got[0].delivery_id == first['id'] and inbox.got[0].text == 'Say hello'

        # The connection drops; what is said meanwhile arrives after the resume, and nothing is identified again.
        gateway.drop()
        missed = message('Are you there?')
        seq = gateway.dispatch('MESSAGE_CREATE', missed)
        await until(lambda: len(inbox.got) == 2, 'the missed message')
        assert inbox.got[1].delivery_id == missed['id']
        assert gateway.resumes == [{'token': TOKEN, 'session_id': 'session-1', 'seq': seq - 1}]
        assert len(gateway.identifies) == 1

        # Discord asks for a new connection: resumed. Then the session is invalid: a fresh Identify.
        gateway.send(7)
        await until(lambda: len(gateway.resumes) == 2, 'a second resume')
        gateway.send(9, False)
        await until(lambda: len(gateway.identifies) == 2, 'a fresh Identify', timeout=30)
        later = message('Still there?')
        gateway.dispatch('MESSAGE_CREATE', later)
        await until(lambda: len(inbox.got) == 3, 'a message in the new session')


async def test_an_event_that_fails_to_land_comes_again_and_counts_once(
    channel: DiscordChannel, gateway: FakeGateway
) -> None:
    inbox = Inbox()
    inbox.failures = 1
    async with listening(channel, inbox):
        await until(lambda: gateway.identifies, 'Identify')
        sent = message('Say hello')
        gateway.dispatch('MESSAGE_CREATE', sent)
        await until(lambda: gateway.resumes, 'a resume')
        await until(lambda: inbox.got, 'the message, again')
        await asyncio.sleep(0.2)
        assert [i.delivery_id for i in inbox.got] == [sent['id']]


async def test_a_connection_without_heartbeat_acks_is_left_and_resumed(api: DiscordAPI, gateway: FakeGateway) -> None:
    gateway.heartbeat_ms = 100
    channel = DiscordChannel(TOKEN, api_url=api.url, files_url=api.url, backoff=0.05)
    try:
        async with listening(channel, Inbox()):
            await until(lambda: gateway.heartbeats >= 2, 'heartbeats')
            gateway.acks = False
            await until(lambda: gateway.resumes, 'a resume of the zombie connection')
            assert len(gateway.identifies) == 1
    finally:
        await channel.aclose()


async def test_a_refused_bot_does_not_hammer_the_gateway(channel: DiscordChannel, gateway: FakeGateway) -> None:
    async with listening(channel, Inbox()):
        await until(lambda: gateway.identifies, 'Identify')
        gateway.close_with(4014)  # disallowed intents: Message Content is off in the Developer Portal
        await asyncio.sleep(1.5)
        assert len(gateway.identifies) == 1 and not gateway.resumes and not gateway.connected


# --- one replica connects ---


async def test_only_the_replica_holding_the_lock_runs_and_another_takes_over(database_url: str) -> None:
    running: list[str] = []

    def work(name: str) -> Callable[[], Coroutine[object, object, None]]:
        async def forever() -> None:
            running.append(name)
            await asyncio.Event().wait()

        return forever

    key = 'sammy:channel:test-leader'
    one = asyncio.create_task(lead(database_url, key, work('one'), retry=0.1, check=0.2))
    await until(lambda: running, 'a leader')
    two = asyncio.create_task(lead(database_url, key, work('two'), retry=0.1, check=0.2))
    await asyncio.sleep(1)
    assert running == ['one']  # the second replica keeps trying, and waits
    one.cancel()
    await asyncio.gather(one, return_exceptions=True)
    await until(lambda: running == ['one', 'two'], 'the other replica to take over')
    two.cancel()
    await asyncio.gather(two, return_exceptions=True)
