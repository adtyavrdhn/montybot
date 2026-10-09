"""#126: a long chat is summarised in the background instead of cut off. Each run sees the summary and no more than
a window and a batch of the latest messages, and Sammy can still read the oldest ones word for word."""

from __future__ import annotations

import psycopg
import pytest
from conftest import Client
from helpers import eventually
from psycopg.types.json import Jsonb
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart

pytestmark = pytest.mark.scripted

PARTY = 'We agreed the party is at the boathouse on Saturday at 7.'


@pytest.fixture
def app_env() -> dict[str, str]:
    return {'HISTORY_SUMMARY_MODEL': 'script:e2e.scripts:summarizer'}


def chat(database_url: str, thread_id: str, count: int) -> None:
    """Add turns to the thread until it has `count` messages, as if the user had kept writing notes. The user's
    message at position 10 is `PARTY`."""
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT count(*) FROM sammy.messages WHERE thread_id = %s', (thread_id,)).fetchone()
        assert row is not None
        for position in range(row[0], count, 2):
            text = PARTY if position == 10 else f'Note {position}: buy apples.'
            turn = [ModelRequest(parts=[UserPromptPart(text)]), ModelResponse(parts=[TextPart('Noted.')])]
            for offset, payload in enumerate(ModelMessagesTypeAdapter.dump_python(turn, mode='json')):
                connection.execute(
                    'INSERT INTO sammy.messages (thread_id, position, payload) VALUES (%s, %s, %s)',
                    (thread_id, position + offset, Jsonb(payload)),
                )


def outside_summary(database_url: str, thread_id: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            'SELECT (SELECT count(*) FROM sammy.messages m WHERE m.thread_id = t.id) - t.compacted_up_to '
            'FROM sammy.threads t WHERE t.id = %s',
            (thread_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_a_long_chat_is_summarised_and_its_oldest_messages_read_back(client: Client, database_url: str) -> None:
    client.sign_up()
    thread = client.ask('Say hello.')
    client.wait_for_reply(thread)
    chat(database_url, thread, 300)

    client.ask('What did I say in message 10?', thread)
    assert client.wait_for_reply(thread) == f'You said: #10 user: {PARTY}'

    # That run done, the chat is summarised in the background, a batch at a time, down to a window and a batch.
    eventually(lambda: outside_summary(database_url, thread) <= 100 or None, what='the chat to be summarised')
    client.ask('How much of our chat do you see?', thread)
    reply = client.wait_for_reply(thread)
    assert reply.endswith(' of your messages and a summary of the rest.')
    assert 25 <= int(reply.split()[2]) <= 51  # 2-message turns: at most a window and a batch, and this message
