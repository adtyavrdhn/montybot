"""#4: every store function a request reaches, called as user B, sees nothing of user A's. Sign-ins are encrypted per
user, and the lease lets one run at a time hold a user's sign-ins."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from montybot import crypto, memory, signins, store
from montybot.browser.state import BrowserState, Cookie
from montybot.db import Pool, create_pool, migrate

pytestmark = pytest.mark.anyio
KEY = crypto.deployment_key(crypto.new_key())


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


async def test_user_b_sees_nothing_of_user_a(pool: Pool) -> None:
    async with pool.connection() as c:
        a = await store.create_user(c, 'a@example.test', 'x')
        b = await store.create_user(c, 'b@example.test', 'x')
        assert a is not None and b is not None
        thread = await store.create_thread(c, a.id, 'groceries')
        run_id = str(uuid.uuid4())
        await store.create_run(c, run_id=run_id, user_id=a.id, thread_id=thread.id, prompt='p', trigger='message')
        await store.append_history(c, thread.id, [ModelRequest(parts=[UserPromptPart(content='hello')])])
        await store.set_run_status(c, run_id, 'waiting')
        ask_id = str(uuid.uuid4())
        await store.create_ask(
            c, ask_id=ask_id, run_id=run_id, user_id=a.id, occurrence=1, kind='question', prompt='?', details={}
        )
        await store.add_activity(c, run_id, 'Opening shop.test')
        memory_id = await memory.add_memory(c, a.id, 'likes brown eggs')

        assert await store.get_thread(c, b.id, thread.id) is None
        assert await store.list_threads(c, b.id) == []
        assert await store.get_run(c, b.id, run_id) is None
        assert await store.latest_run(c, b.id, thread.id) is None
        assert await store.list_history(c, b.id, thread.id) is None
        assert await store.open_ask(c, b.id, run_id) is None
        assert await store.get_ask(c, b.id, ask_id) is None
        assert await store.answer_ask(c, b.id, ask_id, {'text': 'red'}) is None
        assert await store.list_activity(c, b.id, run_id) == []
        assert await memory.search_memories(c, b.id, 'eggs') == []
        assert await memory.list_memories(c, b.id) == []
        assert await memory.delete_memory(c, b.id, memory_id) is False

        # and A still sees all of it
        assert await store.get_thread(c, a.id, thread.id) is not None
        assert await store.open_ask(c, a.id, run_id) is not None
        assert await memory.search_memories(c, a.id, 'eggs') == ['likes brown eggs']


async def test_sign_ins_are_encrypted_per_user(pool: Pool) -> None:
    async with pool.connection() as c:
        a = await store.create_user(c, 'a@example.test', 'x')
        b = await store.create_user(c, 'b@example.test', 'x')
    assert a is not None and b is not None
    state = BrowserState(
        url='https://shop.test/cart',
        cookies=[Cookie(name='sid', value='s3cret-session', domain='shop.test', http_only=True)],
        local_storage={'https://shop.test': {'cart': '["eggs"]'}},
    )
    jar = signins.PostgresJar(pool, KEY)
    await jar.save(user_id=a.id, state=state)
    await jar.save(user_id=a.id, state=state)

    assert await jar.load(user_id=a.id) == state
    assert await jar.load(user_id=b.id) is None
    async with pool.connection() as c:
        row = await (await c.execute('SELECT state, version FROM montybot.sign_ins')).fetchone()
        assert row is not None and row['version'] == 2
        sealed = bytes(row['state'])
        assert b's3cret-session' not in sealed and b'shop.test' not in sealed

        # A's ciphertext in B's row does not open: the user id is bound into it, and the keys differ.
        await c.execute('INSERT INTO montybot.sign_ins (user_id, version, state) VALUES (%s, 1, %s)', (b.id, sealed))
    with pytest.raises(Exception):  # noqa: B017  cryptography's InvalidTag
        await jar.load(user_id=b.id)

    # Another deployment key opens nothing.
    with pytest.raises(Exception):  # noqa: B017
        await signins.PostgresJar(pool, crypto.deployment_key(crypto.new_key())).load(user_id=a.id)


async def test_one_run_at_a_time_holds_a_users_sign_ins(pool: Pool) -> None:
    async with pool.connection() as c:
        a = await store.create_user(c, 'a@example.test', 'x')
    assert a is not None
    lease = signins.PostgresLease(pool)
    assert await lease.acquire(user_id=a.id, run_id='run-1')
    assert await lease.acquire(user_id=a.id, run_id='run-1')  # again, as on a retry
    assert not await lease.acquire(user_id=a.id, run_id='run-2')
    await lease.release(user_id=a.id, run_id='run-2')  # not the holder: no effect
    assert await lease.holder(user_id=a.id) == 'run-1'
    await lease.release(user_id=a.id, run_id='run-1')
    assert await lease.acquire(user_id=a.id, run_id='run-2')

    # A lease that outlived its run expires, so the user is not locked out for good.
    short = signins.PostgresLease(pool, seconds=-1)
    assert await short.acquire(user_id=a.id, run_id='run-2')
    assert await lease.holder(user_id=a.id) is None
    assert await lease.acquire(user_id=a.id, run_id='run-3')
