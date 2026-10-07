"""#4: every store function a request reaches, called as user B, sees nothing of user A's. Sign-ins are encrypted per
user, and the lease lets one run at a time hold a user's sign-ins. #21: B's code cannot reach A's files."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from pydantic_ai.messages import ModelRequest, UserPromptPart

from montybot import crypto, memory, schedules, signins, store
from montybot.browser.state import BrowserState, Cookie
from montybot.db import Pool, create_pool, migrate
from montybot.workspaces import WorkspaceFiles, Workspaces, save_download

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
        schedule = await store.create_schedule(
            c,
            schedule_id=str(uuid.uuid4()),
            user_id=a.id,
            name='groceries',
            cron='0 9 * * 1',
            timezone='UTC',
            when='Mondays at 09:00',
            prompt='fill my cart',
            watch=False,
        )

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
        assert await store.get_schedule(c, b.id, schedule.id) is None
        assert await store.list_schedules(c, b.id) == []
        assert await store.delete_schedule(c, b.id, schedule.id) is False
        assert await store.get_thread(c, b.id, schedule.thread_id) is None

        # and A still sees all of it
        assert await store.get_thread(c, a.id, thread.id) is not None
        assert await store.open_ask(c, a.id, run_id) is not None
        assert await memory.search_memories(c, a.id, 'eggs') == ['likes brown eggs']
        assert await store.list_schedules(c, a.id) == [schedule]

    # Pausing, resuming and deleting check the owner before they reach DBOS.
    assert await schedules.list_for(pool, b.id) == []
    assert await schedules.set_paused(pool, b.id, schedule.id, True) is None
    assert await schedules.set_paused(pool, b.id, schedule.id, False) is None
    assert await schedules.delete(pool, b.id, schedule.id) is False
    assert await schedules.set_paused(pool, b.id, 'not-a-uuid', True) is None
    async with pool.connection() as c:
        assert await store.get_schedule(c, a.id, schedule.id) == schedule


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

        # The label binds a ciphertext to its user and version: the right key alone does not open it elsewhere.
        a_key = await signins.user_key(c, KEY, a.id)
        assert crypto.open_sealed(a_key, sealed, label=signins.state_label(a.id, 2))
        with pytest.raises(InvalidTag):
            crypto.open_sealed(a_key, sealed, label=signins.state_label(b.id, 2))

    # An older version put back in place does not load as the current one.
    async with pool.connection() as c:
        await jar.save(user_id=a.id, state=BrowserState(url='https://shop.test/'))  # version 3, after forgetting
        await c.execute('UPDATE montybot.sign_ins SET state = %s WHERE user_id = %s', (sealed, a.id))
    with pytest.raises(signins.UnreadableSignIns):
        await jar.load(user_id=a.id)

    # Another deployment key opens nothing.
    with pytest.raises(signins.UnreadableSignIns):
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
    assert await lease.holds(user_id=a.id, run_id='run-1')
    await lease.release(user_id=a.id, run_id='run-1')
    assert await lease.acquire(user_id=a.id, run_id='run-2')

    # A lease that outlived its run expires, so the user is not locked out for good.
    short = signins.PostgresLease(pool, seconds=-1)
    assert await short.acquire(user_id=a.id, run_id='run-2')
    assert not await lease.holds(user_id=a.id, run_id='run-2')
    assert await lease.acquire(user_id=a.id, run_id='run-3')

    # Renewing never takes a free lease: a stopped run must not lock the user's browser again.
    await lease.release(user_id=a.id, run_id='run-3')
    await lease.renew(user_id=a.id, run_id='run-3')
    assert not await lease.holds(user_id=a.id, run_id='run-3')


async def test_runs_sharing_one_browser_share_the_lease_on_one_server_only(pool: Pool) -> None:
    async with pool.connection() as c:
        a = await store.create_user(c, 'a@example.test', 'x')
    assert a is not None
    here, there = signins.PostgresLease(pool, owner='vm-1'), signins.PostgresLease(pool, owner='vm-2')
    assert await here.acquire(user_id=a.id, run_id='run-1', shared=True)
    assert await here.acquire(user_id=a.id, run_id='run-2', shared=True)  # a tab of the same browser
    assert not await here.acquire(user_id=a.id, run_id='run-3')  # an engine without tabs: a browser of its own
    assert not await there.acquire(user_id=a.id, run_id='run-4', shared=True)  # another server: another browser
    forget = signins.PostgresLease(pool, seconds=60)  # forgetting a site edits the jar alone
    assert not await forget.acquire(user_id=a.id, run_id='forget:1')
    await here.release(user_id=a.id, run_id='run-1')
    await here.release(user_id=a.id, run_id='run-2')
    assert await forget.acquire(user_id=a.id, run_id='forget:1')
    assert not await here.acquire(user_id=a.id, run_id='run-5', shared=True)  # nor shares it


async def test_user_b_cannot_reach_user_a_files(tmp_path: Path) -> None:
    workspaces = Workspaces(tmp_path)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    saved = await save_download(workspaces.files(a), 'invoice.csv', b'item,quantity,unit_price\n')
    assert saved == '/work/downloads/invoice.csv'
    b_files = WorkspaceFiles(workspaces.of(b))

    async def read_as_b(path: str) -> Any:
        return await b_files(name='Path.read_text', args=(PurePosixPath(path),), kwargs={}, is_async=True)

    with pytest.raises(FileNotFoundError):
        await read_as_b(saved)
    # A link in B's folder, as a program B ran could leave there, does not lead out of it.
    (workspaces.directory(b) / 'a').symlink_to(workspaces.directory(a))
    (workspaces.directory(b) / 'hosts').symlink_to('/etc/hosts')
    for path in ['/work/a/downloads/invoice.csv', '/work/hosts', f'/work/../{a}/downloads/invoice.csv', '/etc/hosts']:
        with pytest.raises(PermissionError):
            await read_as_b(path)
    # A user id names one folder, never a path.
    with pytest.raises(ValueError):
        workspaces.of(f'../{a}')


async def test_thread_history_and_status_share_a_snapshot(pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelResponse, TextPart
    from starlette.requests import Request

    from montybot import api

    async with pool.connection() as connection:
        user = await store.create_user(connection, 'snapshot@example.test', 'x')
        assert user is not None
        thread = await store.create_thread(connection, user.id, 'snapshot')
        run_id = str(uuid.uuid4())
        await store.create_run(
            connection, run_id=run_id, user_id=user.id, thread_id=thread.id, prompt='hello', trigger='message'
        )
    original = store.list_runs

    async def complete_between_reads(connection: Any, user_id: str, thread_id: str) -> list[Any]:
        runs = await original(connection, user_id, thread_id)
        async with pool.connection() as writer:
            await store.append_history(
                writer,
                thread_id,
                [
                    ModelRequest(parts=[UserPromptPart(content='hello')]),
                    ModelResponse(parts=[TextPart(content='reply')]),
                ],
            )
            await store.finish_run(writer, run_id, 'done', output='reply')
        return runs

    monkeypatch.setattr(store, 'list_runs', complete_between_reads)
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/',
            'headers': [],
            'path_params': {'thread_id': thread.id},
            'session': {'user_id': user.id},
            'state': {'resources': SimpleNamespace(pool=pool)},
        }
    )
    response = await api.read_thread(request)
    data = json.loads(bytes(response.body))
    assert data['run']['status'] == 'queued'
    assert data['messages'] == [{'role': 'user', 'text': 'hello'}]
    monkeypatch.setattr(store, 'list_runs', original)
    data = json.loads(bytes((await api.read_thread(request)).body))
    assert data['run']['status'] == 'done'
    assert data['messages'][-1] == {'role': 'assistant', 'text': 'reply'}


async def test_saved_browser_data_includes_storage_and_forgets_subdomains(pool: Pool) -> None:
    import json
    from types import SimpleNamespace

    from starlette.requests import Request

    from montybot import api
    from montybot.browser.state import BLANK_URL, BrowserState, Cookie

    async with pool.connection() as connection:
        user = await store.create_user(connection, 'data@example.test', 'x')
        assert user is not None
    state = BrowserState(
        url='https://auth.example.test/account',
        cookies=[
            Cookie(name='parent', value='dummy', domain='.example.test'),
            Cookie(name='child', value='dummy', domain='auth.example.test'),
            Cookie(name='unrelated', value='dummy', domain='other.test'),
        ],
        local_storage={
            'https://auth.example.test': {'dummy': 'dummy'},
            'https://storage-only.test': {'dummy': 'dummy'},
        },
        session_storage={'https://example.test': {'dummy': 'dummy'}},
    )

    class Jar:
        async def load(self, *, user_id: str) -> BrowserState:
            assert user_id == user.id
            return state

        async def save(self, *, user_id: str, state: BrowserState) -> None:
            assert user_id == user.id

    resources = SimpleNamespace(pool=pool, jar=Jar())

    def request(method: str, site: str = '') -> Request:
        return Request(
            {
                'type': 'http',
                'method': method,
                'path': '/',
                'headers': [(b'content-type', b'application/json')],
                'path_params': {'site': site},
                'session': {'user_id': user.id},
                'state': {'resources': resources},
            }
        )

    listed = json.loads(bytes((await api.read_sign_ins(request('GET'))).body))
    assert {'site': 'storage-only.test'} in listed
    assert (await api.forget_sign_in(request('DELETE', 'example.test'))).status_code == 200
    assert [cookie.domain for cookie in state.cookies] == ['other.test']
    assert state.local_storage == {'https://storage-only.test': {'dummy': 'dummy'}}
    assert state.session_storage == {} and state.url == BLANK_URL
    assert (await api.forget_sign_in(request('DELETE', 'storage-only.test'))).status_code == 200
    assert state.local_storage == {}


async def test_an_answered_run_shows_as_working_until_it_carries_on(pool: Pool) -> None:
    """Between the user's answer and the run waking up, the run is still `waiting` in the database, but it waits for
    nobody: the user sees it working, not an empty "waiting"."""
    from montybot import api

    async with pool.connection() as connection:
        user = await store.create_user(connection, 'answered@example.test', 'x')
        assert user is not None
        thread = await store.create_thread(connection, user.id, 'answered')
        run_id = str(uuid.uuid4())
        await store.create_run(
            connection, run_id=run_id, user_id=user.id, thread_id=thread.id, prompt='hello', trigger='message'
        )
        ask_id = str(uuid.uuid4())
        await store.create_ask(
            connection,
            ask_id=ask_id,
            run_id=run_id,
            user_id=user.id,
            occurrence=1,
            kind='question',
            prompt='Which?',
            details={},
        )
        await store.set_run_status(connection, run_id, 'waiting')
        waiting = await api.run_view(connection, user, await store.load_run(connection, run_id))
        assert waiting['status'] == 'waiting' and waiting['ask']['id'] == ask_id
        assert await store.active_runs(connection, user.id) == {thread.id: 'waiting'}
        await store.answer_ask(connection, user.id, ask_id, {'text': 'that one'})
        answered = await api.run_view(connection, user, await store.load_run(connection, run_id))
        assert answered['status'] == 'running' and answered['ask'] is None
        assert answered['prompt'] == 'hello'  # what the apps send again to try a task again
        assert await store.active_runs(connection, user.id) == {thread.id: 'running'}  # the chat list agrees


async def test_a_run_for_a_deleted_chat_says_so(pool: Pool) -> None:
    """A schedule deleted while its occurrence starts takes its chat; the new run must fail cleanly."""
    async with pool.connection() as connection:
        user = await store.create_user(connection, 'gone@example.test', 'x')
        assert user is not None
        with pytest.raises(store.ThreadGone):
            await store.create_run(
                connection,
                run_id=str(uuid.uuid4()),
                user_id=user.id,
                thread_id=str(uuid.uuid4()),
                prompt='hi',
                trigger='schedule',
            )


async def test_chats_are_listed_by_when_they_were_last_active(pool: Pool) -> None:
    """The apps group chats by `updated_at` and show them in the list's order: the two must agree, including for a
    schedule's chat that has no run yet."""
    async with pool.connection() as connection:
        user = await store.create_user(connection, 'order@example.test', 'x')
        assert user is not None
        older = await store.create_thread(connection, user.id, 'older')
        await store.create_run(
            connection, run_id=str(uuid.uuid4()), user_id=user.id, thread_id=older.id, prompt='hi', trigger='message'
        )
        await connection.execute(
            "UPDATE montybot.runs SET created_at = now() - interval '2 days' WHERE thread_id = %s", (older.id,)
        )
        scheduled = await store.create_thread(connection, user.id, 'a schedule, not run yet')
        listed = [thread.id for thread in await store.list_threads(connection, user.id)]
        last_active = await store.last_active(connection, user.id)
    assert listed == [scheduled.id, older.id]
    assert last_active[scheduled.id] > last_active[older.id]
