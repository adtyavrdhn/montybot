"""The shared chat layer's pure parts and link codes (`sammy.channels`); the flows are in `e2e/test_channels.py`."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fake_channel import FakeChannel
from psycopg.rows import dict_row
from pydantic import SecretStr

from sammy.channels import linking
from sammy.channels import store as channel_store
from sammy.channels.base import ButtonAnswer, button_data
from sammy.channels.registry import Channels
from sammy.channels.text import split
from sammy.db import Connection, migrate
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
    assert await channel_store.take_user_code(connection, 'other', linking.hash_code(code)) is None
    assert await channel_store.take_sender_code(connection, linking.hash_code(code)) is None
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(code)) == user_id
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(code)) is None
    expired = await linking.code_for_user(connection, user_id, 'fake')
    await expire_all(connection)
    assert await channel_store.take_user_code(connection, 'fake', linking.hash_code(expired)) is None
    assert linking.code_in('hello there') is None and linking.code_in('/start') is None
