"""The shared chat layer's pure parts and link codes (`sammy.channels`); the flows are in `e2e/test_channels.py`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString

import pytest
from fake_channel import FakeChannel
from psycopg.rows import dict_row
from pydantic import SecretStr
from starlette.responses import Response

from sammy.channels import linking
from sammy.channels import store as channel_store
from sammy.channels.base import (
    Button,
    ButtonAnswer,
    Capabilities,
    Inbound,
    InboundFile,
    Outgoing,
    RawRequest,
    button_data,
)
from sammy.channels.outbound import Pump
from sammy.channels.registry import Channels
from sammy.channels.store import Chat
from sammy.channels.text import split
from sammy.crypto import new_key
from sammy.db import Connection, migrate
from sammy.resources import open_resources
from sammy.settings import Settings

pytestmark = pytest.mark.anyio
SECRET = 'test-session-secret'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


# --- splitting ---


def test_a_long_reply_is_split_between_paragraphs_then_lines_then_words() -> None:
    assert split('', 10) == [] and split('   \n\n ', 10) == []
    assert split('short', 10) == ['short']
    assert split('one para\n\ntwo para\n\nthree', 20) == ['one para\n\ntwo para', 'three']
    assert split('line one\nline two\nline three', 18) == ['line one\nline two', 'line three']
    assert split('alpha beta gamma delta', 11) == ['alpha beta', 'gamma delta']
    assert split('abcdefghij', 4) == ['abcd', 'efgh', 'ij']
    text = ('word ' * 300 + '\n\n') * 3
    parts = split(text, 200)
    assert all(0 < len(p) <= 200 for p in parts)
    assert ' '.join(' '.join(parts).split()) == ' '.join(text.split())  # nothing lost
    assert split(text, 200) == parts  # the same every time, as a replayed delivery needs
    with pytest.raises(ValueError):
        split('x', 0)


def test_buttons_carry_short_answers() -> None:
    ask_id = '0b4a0e1c-1f7e-5d0e-9f3a-0c4d2b9e7a11'
    for choice in ('approve', 'decline'):
        data = button_data(ask_id, choice)
        assert len(data.encode()) < 64  # Telegram's limit
        assert ButtonAnswer.from_data(data) == ButtonAnswer(ask_id=ask_id, choice=choice)
    assert ButtonAnswer.from_data(f'{ask_id}:delete') is None
    assert ButtonAnswer.from_data(':approve') is None


# --- the registry ---


def settings(backends: list[str]) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url='postgresql://unused',
        session_secret=SecretStr(SECRET),
        encryption_key=SecretStr('unused'),
        channel_backends=backends,
    )


def test_a_platform_is_on_only_with_all_its_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('FAKE_CHANNEL_URL', raising=False)
    monkeypatch.setenv('FAKE_CHANNEL_SECRET', 'secret')
    assert Channels.from_settings(settings(['fake_channel:new_channel'])).names == []
    monkeypatch.setenv('FAKE_CHANNEL_URL', 'http://127.0.0.1:9')
    channels = Channels.from_settings(settings(['fake_channel:new_channel']))
    assert channels.names == ['fake'] and isinstance(channels.get('fake'), FakeChannel)
    assert Channels.from_settings(settings([])).names == []
    monkeypatch.setenv('CHANNEL_BACKENDS', 'fake_channel:new_channel, other:factory')
    assert Settings(_env_file=None, database_url='x', session_secret='s', encryption_key='k').channel_backends == [  # pyright: ignore[reportCallIssue, reportArgumentType]
        'fake_channel:new_channel',
        'other:factory',
    ]
    with pytest.raises(ValueError, match='two chat platforms'):
        Channels([FakeChannel('http://127.0.0.1:9', 's'), FakeChannel('http://127.0.0.1:9', 's')])


# --- link codes ---


@pytest.fixture
async def connection(database_url: str) -> AsyncIterator[Connection]:
    await migrate(database_url)
    async with await Connection.connect(database_url, autocommit=True, row_factory=dict_row) as made:
        yield made


async def new_user(connection: Connection) -> str:
    cursor = await connection.execute(
        "INSERT INTO sammy.users (email, password_hash) VALUES ('pat@example.test', 'x') RETURNING id"
    )
    row = await cursor.fetchone()
    assert row is not None
    return str(row['id'])


async def expire_all(connection: Connection) -> None:
    await connection.execute("UPDATE sammy.channel_link_codes SET expires_at = now() - interval '1 second'")


async def test_a_senders_code_is_reused_while_live_then_works_once(connection: Connection) -> None:
    async def code_for(sender: str) -> str:
        return await linking.code_for_sender(
            connection, SECRET, channel='fake', external_user_id=sender, chat_id=f'dm-{sender}'
        )

    first = await code_for('pat')
    assert linking.code_in(first) == first and linking.code_in(f'/start {first.lower()}') == first
    assert await code_for('pat') == first  # writing again: the same code, not one per message
    assert await code_for('sam') != first
    cursor = await connection.execute('SELECT code_hash FROM sammy.channel_link_codes')
    stored = [row['code_hash'] for row in await cursor.fetchall()]
    assert linking.hash_code(first) in stored and first not in str(stored)  # only its hash is kept

    assert await channel_store.take_sender_code(connection, linking.hash_code(first)) == ('fake', 'pat', 'dm-pat')
    assert await channel_store.take_sender_code(connection, linking.hash_code(first)) is None  # once

    late = await code_for('sam')
    await expire_all(connection)
    assert await channel_store.take_sender_code(connection, linking.hash_code(late)) is None
    assert await code_for('sam') != late  # an expired code is not handed out again


async def test_a_web_users_code_works_once_on_its_own_platform(connection: Connection) -> None:
    user_id = await new_user(connection)
    code = await linking.code_for_user(connection, user_id, 'fake')
    assert await channel_store.take_user_code(connection, 'other', linking.hash_code(code), 'anyone') is None
    assert await channel_store.take_sender_code(connection, linking.hash_code(code)) is None
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(code), 'anyone') == user_id
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(code), 'anyone') is None
    expired = await linking.code_for_user(connection, user_id, 'fake')
    await expire_all(connection)
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(expired), 'anyone') is None
    assert linking.code_in('hello there') is None and linking.code_in('/start') is None


async def test_opening_a_senders_link_gives_a_code_only_that_sender_can_use(connection: Connection) -> None:
    # The code a web user gets for someone's link finishes linking only from that chat account, and is not a
    # sender's code: opening someone else's link never links their chat account on its own.
    user_id = await new_user(connection)
    bound = await linking.code_for_user(connection, user_id, 'fake', sender='alice')
    assert await channel_store.take_sender_code(connection, linking.hash_code(bound)) is None
    assert await channel_store.sender_nonce(connection, 'fake', 'alice') is None
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(bound), 'mallory') is None
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(bound), 'alice') == user_id


# --- delivery ---


class Recording:
    """A platform that keeps what it is sent, and fails to format one text."""

    name = 'recording'
    capabilities = Capabilities(max_text=100, max_buttons=2, edits=True, threads=False, max_file_bytes=1000)

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def verify(self, request: RawRequest) -> bool:
        return False

    def challenge(self, request: RawRequest) -> Response | None:
        return None

    def parse(self, request: RawRequest) -> list[Inbound]:
        return []

    def ack(self) -> Response:
        return Response()

    def format(self, markdown: str) -> str:
        if markdown == 'cannot format':
            raise RuntimeError('a bug in a platform adapter')
        return markdown

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        self.sent.append((chat_id, text))
        return str(len(self.sent))

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        return None

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        return ''

    async def download(self, file: InboundFile) -> bytes:
        return b''

    async def aclose(self) -> None:
        return None


RECORDING = Recording()


def recording_channel(settings: Settings) -> Recording:
    return RECORDING


class Windowed(Recording):
    """A platform that may write to a chat only within an hour of its last message, as WhatsApp within 24."""

    name = 'windowed'
    capabilities = Capabilities(
        max_text=100, max_buttons=2, edits=False, threads=False, max_file_bytes=1000, reply_window=timedelta(hours=1)
    )

    async def reopen(self, chat_id: str) -> str:
        self.sent.append((chat_id, 'REOPEN'))
        return str(len(self.sent))


WINDOWED = Windowed()


def windowed_channel(settings: Settings) -> Windowed:
    return WINDOWED


def app_settings(database_url: str, tmp_path: Path, backend: str) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url=database_url,
        session_secret=SecretStr(SECRET),
        encryption_key=SecretStr(new_key()),
        model='test',
        browser_backend='sammy.browser.fake:FakeBrowser',
        mac_tunnel=False,
        monty_url=None,
        workspaces_dir=tmp_path,
        composio_api_key=None,
        channel_backends=[backend],
    )


async def test_a_message_that_fails_for_any_reason_does_not_hold_back_its_chat(
    database_url: str, tmp_path: Path
) -> None:
    # The pump sends each chat's oldest unsent message first, so a message whose delivery fails anywhere (here in the
    # adapter's formatting, before any platform call) must be marked failed, or the chat would never get another.
    RECORDING.sent.clear()
    configured = app_settings(database_url, tmp_path, 'test_channels:recording_channel')
    async with open_resources(configured) as resources:
        chat = Chat(channel='recording', chat_id='chat-1')
        async with resources.pool.connection() as connection:
            await channel_store.enqueue(connection, 'first', chat, Outgoing(text='cannot format'))
            await channel_store.enqueue(connection, 'second', chat, Outgoing(text='after it'))
        pump = Pump(resources)
        deadline = time.monotonic() + 60
        while not RECORDING.sent and time.monotonic() < deadline:
            await pump.pump()
            await asyncio.sleep(0.2)
        assert RECORDING.sent == [('chat-1', 'after it')]
        async with resources.pool.connection() as connection:
            cursor = await connection.execute(
                'SELECT error FROM sammy.channel_outbox WHERE failed_at IS NOT NULL ORDER BY seq'
            )
            assert [row['error'] for row in await cursor.fetchall()] == ['RuntimeError']


async def test_outside_a_reply_window_the_chat_is_reopened_once_and_its_messages_wait(
    database_url: str, tmp_path: Path
) -> None:
    # A platform with a reply window (WhatsApp's 24 hours): outside it, only its re-engagement message goes, once per
    # closed window, and the chat's messages wait, in order, until the chat writes again.
    WINDOWED.sent.clear()
    chat = Chat(channel='windowed', chat_id='chat-1')
    async with open_resources(app_settings(database_url, tmp_path, 'test_channels:windowed_channel')) as resources:
        pump = Pump(resources)

        async def sql(query: LiteralString) -> int:
            async with resources.pool.connection() as connection:
                cursor = await connection.execute(query)
                row = await cursor.fetchone()
            return 0 if row is None else int(row['n'])

        async def held(releases: int = 0) -> bool:
            query: LiteralString = (
                "SELECT count(*) AS n FROM sammy.channel_outbox WHERE channel = 'windowed' AND held_at IS NOT NULL "
                'AND releases = '
            )
            return await sql(query + ('1' if releases else '0')) == 2

        async def pump_until(done: Callable[[], Awaitable[bool]]) -> None:
            deadline = time.monotonic() + 60
            while not await done():
                assert time.monotonic() < deadline, 'timed out'
                await pump.pump()
                await asyncio.sleep(0.2)

        async def enqueue(*texts: str) -> None:
            async with resources.pool.connection() as connection:
                for text in texts:
                    await channel_store.enqueue(connection, f'w:{text}', chat, Outgoing(text=text))

        async def seen(at: datetime | None = None) -> int:
            async with resources.pool.connection() as connection, connection.transaction():
                return await channel_store.seen(connection, chat, at)

        async def sent(count: int) -> bool:
            return len(WINDOWED.sent) >= count

        # The chat never wrote: one re-engagement message, and both messages held.
        await enqueue('first', 'second')
        await pump_until(held)
        assert WINDOWED.sent == [('chat-1', 'REOPEN')]

        # It writes: the held messages go, in order; while the window is open, so does the next one.
        assert await seen() == 2
        await pump_until(lambda: sent(3))
        await enqueue('third')
        await pump_until(lambda: sent(4))
        assert [text for _, text in WINDOWED.sent] == ['REOPEN', 'first', 'second', 'third']

        # Two hours later the window is closed again: one more re-engagement message.
        await sql(
            "UPDATE sammy.channel_windows SET last_inbound_at = last_inbound_at - interval '2 hours', "
            "reopened_at = reopened_at - interval '2 hours' RETURNING 0 AS n"
        )
        await enqueue('fourth', 'fifth')
        await pump_until(held)
        assert [text for _, text in WINDOWED.sent][4:] == ['REOPEN']

        # A late delivery of an old message moves nothing back: the messages are held again, with no second reopen.
        assert await seen(datetime.now(UTC) - timedelta(hours=3)) == 2
        await pump_until(lambda: held(releases=1))
        assert [text for _, text in WINDOWED.sent].count('REOPEN') == 2

        assert await seen() == 2
        await pump_until(lambda: sent(7))
        assert [text for _, text in WINDOWED.sent] == [
            'REOPEN',
            'first',
            'second',
            'third',
            'REOPEN',
            'fourth',
            'fifth',
        ]
        assert (
            await sql("SELECT count(*) AS n FROM sammy.channel_outbox WHERE channel = 'windowed' AND sent_at IS NULL")
            == 0
        )
