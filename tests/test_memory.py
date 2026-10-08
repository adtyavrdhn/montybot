"""Memory search and recall against Postgres: any word of a query may match, and recall keeps the newest memories
when a request matches none, so a later turn still sees what an earlier one answered from."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from montybot import memory, store
from montybot.db import Pool, create_pool, migrate

pytestmark = pytest.mark.anyio

WORK = 'Works at Pydantic on Pydantic AI'
WALMART = 'Was an AI engineer at Walmart from 2021 to 2026'
BIRTHDAY = 'Birthday is December 21, 1984'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    await migrate(database_url)
    pool = create_pool(database_url)
    await pool.open()
    yield pool
    await pool.close()


async def user_with(pool: Pool, *memories: str) -> str:
    """A user who learned `memories` in this order, each in its own transaction so each is newer than the last."""
    async with pool.connection() as c:
        user = await store.create_user(c, 'a@example.test', 'x')
    assert user is not None
    for text in memories:
        async with pool.connection() as c:
            await memory.add_memory(c, user.id, text)
    return user.id


async def test_search_matches_any_word(pool: Pool) -> None:
    user_id = await user_with(pool, WORK, WALMART, BIRTHDAY)
    async with pool.connection() as c:
        assert await memory.search_memories(c, user_id, 'where do I work') == [WORK]
        # Several topics at once find each of them, not only a memory that holds every word.
        found = await memory.search_memories(c, user_id, 'user birthday work history')
        assert sorted(found) == sorted([WORK, BIRTHDAY])
        # More matching words rank first.
        assert (await memory.search_memories(c, user_id, 'pydantic ai'))[:2] == [WORK, WALMART]
        assert await memory.search_memories(c, user_id, 'where is it') == []  # stop words only


async def test_recall_keeps_the_newest_memories_when_nothing_matches(pool: Pool) -> None:
    user_id = await user_with(pool, WORK, WALMART, BIRTHDAY)
    async with pool.connection() as c:
        assert await memory.recall_memories(c, user_id, 'okay do it') == [BIRTHDAY, WALMART, WORK]
        assert await memory.recall_memories(c, user_id, '') == [BIRTHDAY, WALMART, WORK]
        # A match comes first, however old, and the limit keeps the newest of the rest.
        assert await memory.recall_memories(c, user_id, 'where do I work', limit=2) == [WORK, BIRTHDAY]
