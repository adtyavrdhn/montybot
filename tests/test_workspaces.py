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
    assert await save_download(workspace, 'export.csv', b'one') == '/work/downloads/export.csv'
    assert await save_download(workspace, 'export.csv', b'one') == '/work/downloads/export.csv'  # a repeat
    assert await save_download(workspace, 'export.csv', b'two') == '/work/downloads/export (2).csv'
    assert await save_download(workspace, '../../.bashrc', b'x') == '/work/downloads/bashrc'
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
