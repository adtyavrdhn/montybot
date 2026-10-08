# Copied from viktor c1896df (viktor/memory.py). Memories belong to one user; there is no team memory. Database
# calls are DBOS steps, so a recovered run does not write a memory twice.
from __future__ import annotations

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext

from montybot.db import Connection
from montybot.deps import RunDeps
from montybot.resources import current

SEARCH_LIMIT = 5
RECALL_LIMIT = 20

# Searches match any word of the query, so a question ("where do I work") or several topics ("birthday work") still
# find memories; `ts_rank` puts memories that match more words first. `plainto_tsquery` joins its words with ` & `
# only, and a lexeme never holds a space, so turning that into ` | ` is safe.


async def add_memory(connection: Connection, user_id: str, text: str, thread_id: str | None = None) -> str:
    cursor = await connection.execute(
        'INSERT INTO montybot.memories (user_id, thread_id, text) VALUES (%s, %s, %s) RETURNING id',
        (user_id, thread_id, text.strip()),
    )
    row = await cursor.fetchone()
    assert row is not None
    return str(row['id'])


async def search_memories(connection: Connection, user_id: str, query: str, limit: int = SEARCH_LIMIT) -> list[str]:
    """Memories that match any word of `query`, the best matches first."""
    cursor = await connection.execute(
        """
        SELECT text FROM montybot.memories,
            (SELECT replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery AS query) AS any_word
        WHERE user_id = %s AND search @@ query
        ORDER BY ts_rank(search, query) DESC, created_at DESC
        LIMIT %s
        """,
        (query, user_id, limit),
    )
    return [row['text'] for row in await cursor.fetchall()]


async def recall_memories(connection: Connection, user_id: str, prompt: str, limit: int = RECALL_LIMIT) -> list[str]:
    """Memories that match `prompt` first, then the newest others: a request that matches nothing still gets the
    memories an earlier turn may have answered from, since instructions are not kept in the history."""
    cursor = await connection.execute(
        """
        SELECT text FROM montybot.memories,
            (SELECT replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery AS query) AS any_word
        WHERE user_id = %s
        ORDER BY search @@ query DESC, ts_rank(search, query) DESC, created_at DESC
        LIMIT %s
        """,
        (prompt, user_id, limit),
    )
    return [row['text'] for row in await cursor.fetchall()]


async def list_memories(connection: Connection, user_id: str) -> list[dict[str, str]]:
    cursor = await connection.execute(
        'SELECT id, text FROM montybot.memories WHERE user_id = %s ORDER BY created_at', (user_id,)
    )
    return [{'id': str(row['id']), 'text': row['text']} for row in await cursor.fetchall()]


async def delete_memory(connection: Connection, user_id: str, memory_id: str) -> bool:
    cursor = await connection.execute(
        'DELETE FROM montybot.memories WHERE user_id = %s AND id = %s', (user_id, memory_id)
    )
    return cursor.rowcount == 1


memory_tools: FunctionToolset[RunDeps] = FunctionToolset(id='memory')


@memory_tools.tool
async def remember(ctx: RunContext[RunDeps], fact: str) -> str:
    """Remember a lasting fact about the user or their preferences, such as the brand of eggs they buy."""
    user_id, thread_id = ctx.deps.user_id, ctx.deps.run.thread_id

    async def step() -> str:
        async with current().pool.connection() as connection:
            await add_memory(connection, user_id, fact, thread_id)
        return 'Remembered.'

    return await DBOS.run_step_async({'name': 'memory.remember'}, step)


@memory_tools.tool
async def memory_search(ctx: RunContext[RunDeps], query: str) -> list[str]:
    """Search what you remember about the user by keywords, such as 'birthday' or 'work job'. Any word may match.
    Search before you say you do not remember something."""
    user_id = ctx.deps.user_id

    async def step() -> list[str]:
        async with current().pool.connection() as connection:
            return await search_memories(connection, user_id, query)

    return await DBOS.run_step_async({'name': 'memory.search'}, step)


async def recall(ctx: RunContext[RunDeps]) -> str:
    """Memories that match the request, then the newest others, as instructions. Read in a step, so a replay sees
    what the first attempt saw."""
    user_id, prompt = ctx.deps.user_id, ctx.deps.run.prompt

    async def step() -> list[str]:
        async with current().pool.connection() as connection:
            return await recall_memories(connection, user_id, prompt)

    memories = await DBOS.run_step_async({'name': 'memory.recall'}, step)
    if not memories:
        return ''
    return (
        f'What you remember about the user (at most {RECALL_LIMIT}; use `memory_search` for anything not here):\n'
        + '\n'.join(f'- {memory}' for memory in memories)
    )
