"""Each user's files: one directory on our server, which everything a run does with files sees (#21).

```
Monty code: Path('/work/invoice.csv').read_text()           run_code (montybot.code)
  Monty OS call, answered on our side                        local Monty, or over Full Monty's WebSocket
    WorkspaceFiles: /work/invoice.csv -> <user dir>/invoice.csv
      Workspace.read_bytes(...)                              pydantic_ai.workspaces, one per user
browser download -> save_download -> /work/downloads/<name>  montybot.browsing.Session.read
file in a message -> save_in -> /work/uploads/<name>         montybot.attachments.into_workspace
```

Monty is the code runner, not the workspace: its file calls reach the user's `Workspace` through the `os=` handler
of `feed_run`. Monty calls the handler from a worker thread with `is_async=True`, and the handler returns a coroutine,
which Monty awaits on our event loop, so the async workspace is called directly, with no blocking portal.

Code sees the files at `/work`, never the directory on our server. A path outside `/work`, or one that leads out of
the user's directory through a symlink, raises `PermissionError`, and errors name the `/work` path, not ours.

The `Workspace` API has no append and no modification time, so appends and `stat` use the checked path on our server
directly; everything else goes through the workspace.

The CPython tier (#6) runs code that can make symlinks and FIFOs in the user's directory. Calls here take the user's
lock, which `run_python` holds while its jail runs, so nothing changes a path between the check and its use; links
that lead out are refused, and only regular files are read or written.
"""

from __future__ import annotations

import asyncio
import errno
import os
import posixpath
import stat as stat_module
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace, WorkspaceError
from pydantic_monty import NOT_HANDLED, MontyFileHandle
from pydantic_monty.os_access import StatResult, path_from_arg

VIRTUAL_ROOT = '/work'
"""Where code sees the user's files."""
DOWNLOADS = f'{VIRTUAL_ROOT}/downloads'
"""Where browser downloads are saved."""
UPLOADS = f'{VIRTUAL_ROOT}/uploads'
"""Where the files the user attaches to their messages are saved."""


MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
"""The most a file Monty gives the user may hold."""


class FileTooLarge(ValueError):
    """The file is over `MAX_DOWNLOAD_BYTES`, the most read into memory to give the user."""


class Workspaces:
    """One `Workspace` per user, each a directory under `root`, made on first use."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, user_id: str) -> asyncio.Lock:
        """Held by each file call and by `run_python` for its whole run, so they never overlap for one user."""
        return self._locks.setdefault(str(uuid.UUID(user_id)), asyncio.Lock())

    def files(self, user_id: str) -> WorkspaceFiles:
        return WorkspaceFiles(self.of(user_id), self.lock(user_id))

    def directory(self, user_id: str) -> Path:
        return self.root / str(uuid.UUID(user_id))  # a user id is a UUID, so it cannot name another directory

    def of(self, user_id: str) -> Workspace:
        directory = self.directory(user_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return Workspace(LocalWorkspaceBackend(directory))


def _contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


_ERRORS: dict[int, type[OSError]] = {
    errno.ENOENT: FileNotFoundError,
    errno.EISDIR: IsADirectoryError,
    errno.ENOTDIR: NotADirectoryError,
    errno.EEXIST: FileExistsError,
    errno.EACCES: PermissionError,
}


def _error(code: int, message: str, path: str) -> OSError:
    return _ERRORS.get(code, OSError)(code, message, path)


def _shown(error: OSError, path: str) -> OSError:
    """The same kind of error about `path`. Some carry no errno (`IsADirectoryError(path)`), so the kind wins."""
    code = error.errno or next((c for c, kind in _ERRORS.items() if isinstance(error, kind)), errno.EIO)
    return _error(code, error.strerror or os.strerror(code), path)


class WorkspaceFiles:
    """Monty's file calls (`pathlib`, `open`) answered from one user's `Workspace`. An `OsHandler` for `feed_run`.

    Calls that are not about files (the clock, the environment) are left to Monty, as without a handler.
    """

    def __init__(self, workspace: Workspace, lock: asyncio.Lock | None = None) -> None:
        self.workspace = workspace
        self.lock = lock or asyncio.Lock()
        self._calls: dict[str, Callable[..., Awaitable[Any]]] = {
            'Path.exists': self.exists,
            'Path.is_file': self.is_file,
            'Path.is_dir': self.is_dir,
            'Path.is_symlink': self.is_symlink,
            'open': self.open,
            'Path.read_text': self.read_text,
            'Path.read_bytes': self.read_bytes,
            'Path.write_text': self.write_text,
            'Path.write_bytes': self.write_bytes,
            'Path.append_text': self.append_text,
            'Path.append_bytes': self.append_bytes,
            'Path.mkdir': self.mkdir,
            'Path.unlink': self.unlink,
            'Path.rmdir': self.rmdir,
            'Path.iterdir': self.iterdir,
            'Path.stat': self.stat,
            'Path.rename': self.rename,
            'Path.resolve': self.resolve,
            'Path.absolute': self.resolve,
        }

    def __call__(
        self, *, name: str, args: tuple[Any, ...], kwargs: dict[str, Any], is_async: bool, **_future: Any
    ) -> Any:
        call = self._calls.get(name)
        if call is None or not is_async:
            return NOT_HANDLED
        return self._answer(call, args, kwargs)

    async def _answer(self, call: Callable[..., Awaitable[Any]], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Run one call, with errors that name the `/work` path and never the directory on our server."""
        shown = str(path_from_arg(args[0])) if args else VIRTUAL_ROOT
        try:
            async with self.lock:
                return await call(*args, **kwargs)
        except OSError as error:
            if error.filename == shown and error.errno is not None:
                raise  # one of ours, already about the /work path
            raise _shown(error, shown) from None
        except UnicodeDecodeError:
            raise  # `read_text` of a file that is not UTF-8; it names no path
        except (WorkspaceError, ValueError):
            raise _error(errno.EIO, 'the file cannot be used', shown) from None

    # --- paths ---

    @staticmethod
    def _virtual(path: PurePosixPath | MontyFileHandle | str) -> str:
        return posixpath.normpath(str(path_from_arg(path) if not isinstance(path, str) else path))

    async def _host(self, path: PurePosixPath | MontyFileHandle | str) -> str:
        """The path on our server for a `/work` path, after following symlinks, which must stay in the directory."""
        virtual = self._virtual(path)
        if not _contains(VIRTUAL_ROOT, virtual):
            raise _error(errno.EACCES, f'Permission denied: only files under {VIRTUAL_ROOT} can be used', virtual)
        root = await self.workspace.working_dir()
        host = posixpath.join(root, posixpath.relpath(virtual, VIRTUAL_ROOT))
        real = await self.workspace.realpath(host)
        if not _contains(root, real):
            raise _error(errno.EACCES, f'Permission denied: it leads out of {VIRTUAL_ROOT}', virtual)
        return real

    async def _not_root(self, path: PurePosixPath) -> str:
        host = await self._host(path)
        if host == await self.workspace.working_dir():
            raise _error(errno.EACCES, f'Permission denied: {VIRTUAL_ROOT} itself cannot be changed', str(path))
        return host

    # --- files given to the user (`montybot.attachments.share_file`) ---

    @staticmethod
    def _directory_fd(path: str, *, parent: int | None = None) -> int:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)

    async def read_result(self, path: str) -> bytes:
        """Read bounded bytes from pinned descriptors, not a checked path reopened later.

        The shared lock excludes CPython. O_NOFOLLOW on every component and fstat on the
        final O_NONBLOCK descriptor also protect against replacement outside that lock.
        """
        try:
            path.encode('utf-8')
        except UnicodeEncodeError:
            raise PermissionError('invalid workspace path') from None
        parts = path.split('/')
        if len(path) > 1024 or parts[:2] != ['', 'work'] or len(parts) < 3:
            raise PermissionError('invalid workspace path')
        if any(part in ('', '.', '..') or '\\' in part or '\x00' in part for part in parts[2:]):
            raise PermissionError('invalid workspace path')
        async with self.lock:
            await self._host(path)  # existing boundary checks; never open the resolved path
            root = await self._host(VIRTUAL_ROOT)

            def read() -> bytes:
                fd = self._directory_fd(root)
                try:
                    for part in parts[2:-1]:
                        child = self._directory_fd(part, parent=fd)
                        os.close(fd)
                        fd = child
                    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        info = os.fstat(file_fd)
                        if not stat_module.S_ISREG(info.st_mode):
                            raise PermissionError('not a regular file')
                        if info.st_size > MAX_DOWNLOAD_BYTES:
                            raise FileTooLarge
                        with os.fdopen(file_fd, 'rb', closefd=False) as stream:
                            data = stream.read(MAX_DOWNLOAD_BYTES + 1)
                        if len(data) > MAX_DOWNLOAD_BYTES:
                            raise FileTooLarge
                        return data
                    finally:
                        os.close(file_fd)
                finally:
                    os.close(fd)

            return await asyncio.to_thread(read)

    # --- the calls ---

    async def exists(self, path: PurePosixPath) -> bool:
        return await self.workspace.exists(await self._host(path))

    async def is_file(self, path: PurePosixPath) -> bool:
        host = await self._host(path)
        return await self.workspace.exists(host) and not (await self.workspace.stat(host)).is_dir

    async def is_dir(self, path: PurePosixPath) -> bool:
        host = await self._host(path)
        return await self.workspace.exists(host) and (await self.workspace.stat(host)).is_dir

    async def is_symlink(self, path: PurePosixPath) -> bool:
        await self._host(path)
        return False  # code sees each file at its own path; links are followed, and only within the directory

    async def open(self, path: PurePosixPath, mode: str) -> MontyFileHandle:
        handle = MontyFileHandle(str(path), mode)  # checks the mode before anything changes
        host = await self._not_root(path)
        exists = await self.workspace.exists(host)
        if exists and (await self.workspace.stat(host)).is_dir:
            raise _error(errno.EISDIR, 'Is a directory', str(path))
        action = handle.mode[0]
        if action == 'r' and not exists:
            raise _error(errno.ENOENT, 'No such file or directory', str(path))
        if action == 'w' or (action == 'a' and not exists):
            await self._write(path, b'')
        return handle

    async def read_bytes(self, path: PurePosixPath | MontyFileHandle) -> bytes:
        return await self.workspace.read_bytes(await self._host(path))

    async def read_text(self, path: PurePosixPath | MontyFileHandle) -> str:
        return (await self.read_bytes(path)).decode()

    async def _writable(self, path: PurePosixPath | MontyFileHandle) -> str:
        """The checked path of a file to write: its folder exists, and it is a regular file or nothing yet."""
        host = await self._not_root(path_from_arg(path))
        if not await self.workspace.exists(posixpath.dirname(host)):  # pathlib does not make missing folders
            raise _error(errno.ENOENT, 'No such file or directory', self._virtual(path))
        try:
            mode = (await asyncio.to_thread(os.lstat, host)).st_mode
        except FileNotFoundError:
            return host
        if stat_module.S_ISDIR(mode):
            raise _error(errno.EISDIR, 'Is a directory', self._virtual(path))
        if not stat_module.S_ISREG(mode):  # such as a FIFO, which would block the write
            raise _error(errno.EINVAL, 'not a regular file', self._virtual(path))
        return host

    async def _write(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> None:
        await self.workspace.write_bytes(await self._writable(path), data)

    async def write_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        await self._write(path, data)
        return len(data)

    async def write_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        await self._write(path, data.encode())
        return len(data)

    async def _append(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> None:
        """Monty sends every `write` after the first as an append, so this adds to the file rather than rewriting it."""
        host = await self._writable(path)

        def append() -> None:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(host, flags, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

        await asyncio.to_thread(append)

    async def append_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        await self._append(path, data)
        return len(data)

    async def append_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        await self._append(path, data.encode())
        return len(data)

    async def mkdir(self, path: PurePosixPath, parents: bool = False, exist_ok: bool = False) -> None:
        host = await self._host(path)
        if await self.workspace.exists(host):
            if exist_ok and (await self.workspace.stat(host)).is_dir:
                return
            raise _error(errno.EEXIST, 'File exists', str(path))
        if not parents and not await self.workspace.exists(posixpath.dirname(host)):
            raise _error(errno.ENOENT, 'No such file or directory', str(path))
        await self.workspace.make_dir(host)

    async def unlink(self, path: PurePosixPath) -> None:
        host = await self._not_root(path)
        if (await self.workspace.stat(host)).is_dir:
            raise _error(errno.EISDIR, 'Is a directory', str(path))
        await self.workspace.remove(host)

    async def rmdir(self, path: PurePosixPath) -> None:
        host = await self._not_root(path)
        if not (await self.workspace.stat(host)).is_dir:
            raise _error(errno.ENOTDIR, 'Not a directory', str(path))
        if await self.workspace.list_dir(host):
            raise _error(errno.ENOTEMPTY, 'Directory not empty', str(path))
        await self.workspace.remove(host)

    async def iterdir(self, path: PurePosixPath) -> list[PurePosixPath]:
        entries = await self.workspace.list_dir(await self._host(path))
        virtual = PurePosixPath(self._virtual(path))
        return [virtual / entry.name for entry in entries]

    async def stat(self, path: PurePosixPath) -> StatResult:
        found = await asyncio.to_thread(os.stat, await self._host(path))
        if stat_module.S_ISDIR(found.st_mode):
            return StatResult.dir_stat(mtime=found.st_mtime)
        return StatResult.file_stat(found.st_size, mtime=found.st_mtime)

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        """Files only: the workspace has no rename, so it is a copy and a delete."""
        host = await self._not_root(path)
        if (await self.workspace.stat(host)).is_dir:
            raise _error(errno.EISDIR, 'only files can be renamed', str(path))
        if await self._host(target) == host:
            return
        data = await self.workspace.read_bytes(host)
        await self._write(target, data)
        await self.workspace.remove(host)

    async def resolve(self, path: PurePosixPath) -> str:
        await self._host(path)
        return self._virtual(path)


def download_name(name: str) -> str:
    """A safe file name from the one a site suggested: no folders, no hidden files, nothing unprintable, and short
    enough for any file system once a ` (2)` is added."""
    name = posixpath.basename(name.replace('\\', '/'))
    name = ''.join(c for c in name if c.isprintable()).strip().lstrip('.')
    while len(name.encode()) > 200:
        name = name[1:]  # the end has the extension
    return name or 'download'


async def save_download(files: WorkspaceFiles, name: str, data: bytes) -> str:
    """Save a browser download in the user's files; returns the path code sees it at (`save_in`)."""
    return await save_in(files, DOWNLOADS, name, data)


async def save_in(files: WorkspaceFiles, folder: str, name: str, data: bytes) -> str:
    """Save a file in a folder of the user's files; returns the path code sees it at. A file of the same name and
    content is kept, so a save repeated after a restart leaves one copy; another with the same name gets ` (2)`,
    ` (3)`... as in a browser."""
    async with files.lock:
        return await _save_in(files, folder, download_name(name), data)


async def _save_in(files: WorkspaceFiles, folder: str, name: str, data: bytes) -> str:
    await files.mkdir(PurePosixPath(folder), parents=True, exist_ok=True)
    stem, dot, extension = name.rpartition('.') if '.' in name else (name, '', '')
    for number in range(1, 1000):
        path = PurePosixPath(folder, name if number == 1 else f'{stem} ({number}){dot}{extension}')
        if not await files.exists(path):
            await files.write_bytes(path, data)
            return str(path)
        if await files.is_file(path) and await files.read_bytes(path) == data:
            return str(path)
    raise _error(errno.EEXIST, 'too many files of this name', f'{folder}/{name}')
