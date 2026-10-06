"""#21: browser downloads in the user's files, and file calls that ordinary code relies on. Isolation between users is
in `test_isolation.py`; the same calls from Monty code are in `test_run_code.py`."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from montybot.workspaces import WorkspaceFiles, Workspaces, download_name, save_download

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def workspaces(tmp_path: Path) -> Workspaces:
    return Workspaces(tmp_path)


async def call(files: WorkspaceFiles, name: str, *args: Any, **kwargs: Any) -> Any:
    """As Monty calls the handler."""
    paths = tuple(PurePosixPath(a) if isinstance(a, str) and a.startswith('/') else a for a in args)
    return await files(name=name, args=paths, kwargs=kwargs, is_async=True)


async def test_downloads_keep_one_copy_and_never_overwrite(workspaces: Workspaces) -> None:
    workspace = workspaces.of(str(uuid.uuid4()))
    assert await save_download(WorkspaceFiles(workspace), 'export.csv', b'one') == '/work/downloads/export.csv'
    assert (
        await save_download(WorkspaceFiles(workspace), 'export.csv', b'one') == '/work/downloads/export.csv'
    )  # a repeat
    assert await save_download(WorkspaceFiles(workspace), 'export.csv', b'two') == '/work/downloads/export (2).csv'
    assert await save_download(WorkspaceFiles(workspace), '../../.bashrc', b'x') == '/work/downloads/bashrc'
    assert len(download_name('發票' * 200 + '.pdf').encode()) <= 200


async def test_file_calls_behave_as_pathlib(workspaces: Workspaces) -> None:
    user = str(uuid.uuid4())
    files = WorkspaceFiles(workspaces.of(user))
    await call(files, 'Path.write_text', '/work/a.txt', 'x')
    await call(files, 'Path.rename', '/work/a.txt', '/work/./a.txt')  # onto itself: nothing changes
    assert await call(files, 'Path.read_text', '/work/a.txt') == 'x'
    for _ in range(3):
        await call(files, 'Path.append_text', '/work/a.txt', 'y')
    assert await call(files, 'Path.read_text', '/work/a.txt') == 'xyyy'

    os.utime(workspaces.directory(user) / 'a.txt', (1_000_000, 1_000_000))
    assert (await call(files, 'Path.stat', '/work/a.txt')).st_mtime == 1_000_000

    await call(files, 'Path.mkdir', '/work/d')
    with pytest.raises(IsADirectoryError):
        await call(files, 'Path.read_text', '/work/d')
    with pytest.raises(FileNotFoundError):
        await call(files, 'Path.write_text', '/work/missing/a.txt', 'x')
    with pytest.raises(PermissionError):
        await call(files, 'Path.rmdir', '/work')


async def test_consumer_results_reject_links_special_files_and_traversal(workspaces: Workspaces) -> None:
    user, other = str(uuid.uuid4()), str(uuid.uuid4())
    files = workspaces.files(user)
    saved = await save_download(files, 'export.csv', b'item,total\neggs,3\n')
    await call(files, 'Path.write_text', '/work/generated.csv', 'total\n3\n')
    root = workspaces.directory(user)
    (root / 'link.csv').symlink_to(root / 'generated.csv')
    (root / 'linked-folder').symlink_to(root / 'downloads')
    workspaces.of(other)
    (root / 'other').symlink_to(workspaces.directory(other))
    os.mkfifo(root / 'pipe')
    listing, truncated = await files.list_results()
    assert not truncated
    assert listing == [
        {'path': saved, 'size': 18},
        {'path': '/work/generated.csv', 'size': 8},
    ]
    assert await files.read_result(saved) == b'item,total\neggs,3\n'
    for path in [
        '/work/link.csv',
        '/work/linked-folder/export.csv',
        '/work/pipe',
        '/work/downloads',
        '/work/other/secret.csv',
        '/work/../generated.csv',
        '/work/downloads/../generated.csv',
        '/etc/passwd',
        '/work//generated.csv',
        '/work/./generated.csv',
        '/work/generated.csv\x00',
    ]:
        with pytest.raises(OSError):
            await files.read_result(path)
    with pytest.raises(FileNotFoundError):
        await workspaces.files(other).read_result(saved)


async def test_consumer_bounds_and_shared_lock(workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from montybot import workspaces as module

    files = workspaces.files(str(uuid.uuid4()))
    for number in range(4):
        await call(files, 'Path.write_text', f'/work/{number}.csv', '12345')
    monkeypatch.setattr(module, 'MAX_LIST_ENTRIES', 2)
    listing, truncated = await files.list_results()
    assert truncated and len(listing) == 2
    monkeypatch.setattr(module, 'MAX_DOWNLOAD_BYTES', 4)
    with pytest.raises(module.FileTooLarge):
        await files.read_result('/work/0.csv')
    # A stale size check must not permit an unbounded read if the file grows.
    original_fstat = os.fstat
    from types import SimpleNamespace

    with monkeypatch.context() as checks:
        checks.setattr(
            os,
            'fstat',
            lambda fd: SimpleNamespace(st_mode=original_fstat(fd).st_mode, st_size=0),
        )
        with pytest.raises(module.FileTooLarge):
            await files.read_result('/work/0.csv')
    async with files.lock:
        read = asyncio.create_task(files.read_result('/work/absent'))
        listing_task = asyncio.create_task(files.list_results())
        await asyncio.sleep(0)
        assert not read.done() and not listing_task.done()
    with pytest.raises(FileNotFoundError):
        await read
    await listing_task


@pytest.mark.parametrize('replacement', ['symlink', 'fifo', 'directory-link'])
async def test_consumer_descriptor_checks_after_path_validation(
    workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    files = workspaces.files(str(uuid.uuid4()))
    await save_download(files, 'result.csv', b'allowed')
    root = Path(await files.workspace.working_dir())
    outside = root.parent / 'outside'
    outside.mkdir()
    (outside / 'result.csv').write_bytes(b'secret')
    original = files._host  # validation is deliberately followed by an adversarial filesystem change

    async def swap(path: Any) -> str:
        host = await original(path)
        if path == '/work/downloads/result.csv':
            if replacement == 'directory-link':
                (root / 'downloads').rename(root / 'old-downloads')
                (root / 'downloads').symlink_to(outside)
            else:
                target = root / 'downloads/result.csv'
                target.unlink()
                if replacement == 'fifo':
                    os.mkfifo(target)
                else:
                    target.symlink_to(outside / 'result.csv')
        return host

    monkeypatch.setattr(files, '_host', swap)
    with pytest.raises(OSError):
        await files.read_result('/work/downloads/result.csv')


def test_consumer_api_downloads_are_authenticated_and_private(
    workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real app routes/session/auth, with only the database lookup replaced."""
    import asyncio
    import base64
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from itsdangerous import TimestampSigner
    from pydantic import SecretStr
    from starlette.testclient import TestClient
    from starlette.types import Receive, Scope, Send

    from montybot import store
    from montybot.app import create_app
    from montybot.models import User
    from montybot.settings import Settings

    alice = User(id=str(uuid.uuid4()), email='a@example.test', name='Alice')
    bob = User(id=str(uuid.uuid4()), email='b@example.test', name='Bob')
    users = {alice.id: alice, bob.id: bob}

    @asynccontextmanager
    async def connection() -> Any:
        yield None

    async def get_user(connection: Any, user_id: str) -> User | None:
        return users.get(user_id)

    monkeypatch.setattr(store, 'get_user', get_user)
    resources = SimpleNamespace(pool=SimpleNamespace(connection=connection), workspaces=workspaces)
    app = create_app(
        Settings(
            database_url='unused',
            session_secret=SecretStr('test-secret'),
            encryption_key=SecretStr('unused'),
        )
    )

    async def injected(scope: Scope, receive: Receive, send: Send) -> None:
        scope['state'] = {'resources': resources}
        await app(scope, receive, send)

    client = TestClient(injected)

    def sign_in(user: User) -> None:
        data = base64.b64encode(json.dumps({'user_id': user.id}).encode())
        cookie = TimestampSigner('test-secret').sign(data).decode()
        client.cookies.set('montybot_session', cookie)

    async def prepare() -> str:
        files = workspaces.files(alice.id)
        saved = await save_download(files, 'export.csv', b'item,total\neggs,3\n')
        await call(files, 'Path.write_text', '/work/generated.csv', 'total\n3\n')
        await call(files, 'Path.write_text', '/work/發票".csv', 'csv')
        return saved

    saved = asyncio.run(prepare())
    try:
        assert client.get('/api/files').status_code == 401
        assert client.post('/api/files/download', json={'path': saved}).status_code == 401
        sign_in(alice)
        listed = client.get('/api/files')
        assert listed.headers['cache-control'] == 'no-store'
        assert saved in [file['path'] for file in listed.json()['files']]
        for path, content in [(saved, b'item,total\neggs,3\n'), ('/work/generated.csv', b'total\n3\n')]:
            response = client.post('/api/files/download', json={'path': path})
            assert response.status_code == 200 and response.content == content
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['content-type'] == 'application/octet-stream'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert response.headers['content-disposition'].startswith("attachment; filename*=UTF-8''")
        response = client.post('/api/files/download', json={'path': '/work/發票".csv'})
        assert '%22.csv' in response.headers['content-disposition']
        assert client.post('/api/files/download', content='{}').status_code == 415
        assert client.post('/api/files/download', json={'path': 'x' * 9000}).status_code == 413
        for path in ['/work/../generated.csv', '/etc/passwd', '/work/downloads/../generated.csv']:
            response = client.post('/api/files/download', json={'path': path})
            assert response.status_code == 404 and path not in response.text
            assert response.headers['cache-control'] == 'no-store'
        sign_in(bob)
        assert client.get('/api/files').json()['files'] == []
        for path in [
            saved,
            f'/work/../{alice.id}/generated.csv',
            str(workspaces.directory(alice.id) / 'generated.csv'),
        ]:
            assert client.post('/api/files/download', json={'path': path}).status_code == 404
    finally:
        client.close()
