"""#6: `run_python` in its bubblewrap jail. It sees the user's files and nothing else of the server: not another user's
files, not the app's folder or environment, not the network; its limits hold; and nothing of it outlives the call.
The jail passes Pydantic AI's `WorkspaceBackendSuite`.

Needs Linux with bwrap and `/usr/bin/python3` with pandas: `tests/linux/run.sh tests/test_cpython.py` runs these in a
Linux container from any Docker host.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from pydantic import SecretStr
from pydantic_ai.workspaces import WorkspaceBackend
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from pydantic_ai_harness.bubblewrap_sandbox import BubblewrapWorkspace

from montybot.cpython import can_jail, jail_of, run_jailed
from montybot.settings import Settings
from montybot.workspaces import Workspaces, save_download

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not can_jail(), reason='bwrap needs Linux: run tests/linux/run.sh tests/test_cpython.py'),
]


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def workspaces_dir() -> Iterator[Path]:
    """Outside `/tmp`, as on the server, because the jail has its own `/tmp` and would hide the folder for free."""
    folder = Path.home() / '.cache' / 'montybot-tests' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    yield folder
    shutil.rmtree(folder)


@pytest.fixture
def resources(workspaces_dir: Path) -> Any:
    settings = Settings(
        database_url='postgresql://unused',
        session_secret=SecretStr('x'),
        encryption_key=SecretStr('x'),
        workspaces_dir=workspaces_dir,
        cpython_cpu_seconds=2,
        cpython_timeout_seconds=30,
    )
    return SimpleNamespace(settings=settings, workspaces=Workspaces(settings.workspaces_dir))


@pytest.fixture
def listener() -> Iterator[int]:
    """A port on this machine that accepts connections, for code in the jail to try."""
    with socket.create_server(('127.0.0.1', 0)) as server:
        yield server.getsockname()[1]


async def test_pandas_works_on_the_users_files(resources: Any) -> None:
    user = str(uuid.uuid4())
    await save_download(
        resources.workspaces.files(user), 'a.csv', b'item,quantity,unit_price\neggs,2,3.20\nmilk,1,1.10\n'
    )
    code = """
import pandas as pd
frame = pd.read_csv('downloads/a.csv')
print(f'{(frame.quantity * frame.unit_price).sum():.2f}')
open('out.txt', 'w').write('written in the jail')
"""
    assert await run_jailed(resources, user, code) == '7.50'
    assert (resources.workspaces.directory(user) / 'out.txt').read_text() == 'written in the jail'


PROBE = """
import json, os, socket
seen = {}
def attempt(name, use):
    try:
        seen[name] = repr(use())
    except Exception as error:
        seen[name] = type(error).__name__
attempt('other user', lambda: open(OTHER).read())
attempt('every user', lambda: os.listdir(ROOT))
attempt('app folder', lambda: os.listdir(APP))
attempt('app environment', lambda: open(f'/proc/{APP_PID}/environ', 'rb').read())
attempt('environment', lambda: [k for k in os.environ if k not in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'HOME', 'PWD', 'OPENBLAS_NUM_THREADS', 'SHLVL', '_')])
attempt('own environ', lambda: b'montybot-secret' in open('/proc/self/environ', 'rb').read())
attempt('processes', lambda: len([p for p in os.listdir('/proc') if p.isdigit()]) < 5)
attempt('network', lambda: socket.create_connection(('127.0.0.1', PORT), timeout=2))
attempt('outside write', lambda: open('/usr/montybot-was-here', 'w'))
print(json.dumps(seen))
"""


async def test_the_jail_sees_only_the_users_files(
    resources: Any, listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('MONTYBOT_PROBE_SECRET', 'montybot-secret')  # as the app's settings would be
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await save_download(resources.workspaces.files(a), 'a.csv', b'only for A')
    other = resources.workspaces.directory(a) / 'downloads' / 'a.csv'
    names = {
        'OTHER': str(other),
        'ROOT': str(resources.workspaces.root),
        'APP': os.getcwd(),
        'APP_PID': os.getpid(),
        'PORT': listener,
    }
    code = ''.join(f'{name} = {value!r}\n' for name, value in names.items()) + PROBE
    seen = json.loads(await run_jailed(resources, b, code))
    assert seen == {
        'other user': 'FileNotFoundError',
        'every user': repr([b]),  # only B's own folder is mounted there
        'app folder': '[]',  # an empty folder over the app's
        'app environment': 'FileNotFoundError',
        'environment': '[]',
        'own environ': 'False',
        'processes': 'True',
        'network': 'PermissionError',
        'outside write': 'OSError',
    }, seen


async def test_limits_hold_and_nothing_outlives_the_call(resources: Any) -> None:
    user = str(uuid.uuid4())
    out = await run_jailed(resources, user, 'bytearray(3 * 2**30)')
    assert 'MemoryError' in out and 'Exit code 1' in out
    out = await run_jailed(resources, user, 'while True:\n    pass')  # two CPU seconds in the test settings
    assert 'Exit code' in out
    forks = 'import os\nfor n in range(100):\n    if os.fork() == 0:\n        os.pause()\n'
    assert 'BlockingIOError' in await run_jailed(resources, user, forks)  # at most a few dozen processes
    tmp = "for n in range(4):\n    open(f'/tmp/{n}', 'wb').write(bytes(90 * 2**20))"
    assert 'No space left on device' in await run_jailed(resources, user, tmp)  # /tmp is small

    code = "import subprocess\nsubprocess.Popen(['sleep', '6123'], start_new_session=True)\nprint('started')"
    assert await run_jailed(resources, user, code) == 'started'
    assert not running('sleep\x006123')  # a detached process ends with the call


def running(marker: str) -> bool:
    """Whether a process on this machine has `marker` on its command line."""
    for cmdline in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            if marker.encode() in cmdline.read_bytes():
                return True
        except OSError:
            pass  # it ended meanwhile
    return False


class TestJailConformance(WorkspaceBackendSuite):
    @pytest.fixture
    def anyio_backend(self) -> str:
        return 'asyncio'

    @pytest.fixture
    async def backend(self, tmp_path: Path) -> AsyncIterator[WorkspaceBackend]:
        yield jail_of(Workspaces(tmp_path), str(uuid.uuid4()))

    async def test_cancellation_stops_foreground_work(
        self, backend: WorkspaceBackend, has_real_posix_shell: bool
    ) -> None:
        """The suite's rule, seen from this machine. The suite reads the command's PID and checks it from a second
        command, but each command here has its own PID namespace, so the second one cannot see the first's."""
        assert isinstance(backend, BubblewrapWorkspace)
        marker = f'{uuid.uuid4().int % 10**9}'

        async def command() -> None:
            await backend.run(['sh', '-c', f'exec sleep 36{marker}'])

        async with anyio.create_task_group() as tg:
            tg.start_soon(command)
            with anyio.fail_after(30):
                while not running(f'36{marker}'):
                    await anyio.sleep(0.05)
            tg.cancel_scope.cancel()
        with anyio.fail_after(60):
            while running(f'36{marker}'):
                await anyio.sleep(0.05)


async def test_what_the_jail_leaves_cannot_lead_the_app_out(resources: Any) -> None:
    """Links and FIFOs the code makes stay in the user's files: Monty's calls refuse them, and do not hang."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await save_download(resources.workspaces.files(a), 'a.csv', b'only for A')
    other = resources.workspaces.directory(a) / 'downloads' / 'a.csv'
    code = f"import os\nos.symlink({str(other)!r}, 'a.csv')\nos.symlink('/etc', 'etc')\nos.mkfifo('pipe')\nprint('ok')"
    assert await run_jailed(resources, b, code) == 'ok'

    files = resources.workspaces.files(b)

    async def call(name: str, *args: Any) -> Any:
        paths = tuple(PurePosixPath(a) if isinstance(a, str) else a for a in args[:1]) + args[1:]
        return await files(name=name, args=paths, kwargs={}, is_async=True)

    for path in ['/work/a.csv', '/work/etc/passwd']:
        with pytest.raises(PermissionError):
            await call('Path.read_text', path)
    with anyio.fail_after(10):
        for name in ['Path.write_text', 'Path.append_text']:
            with pytest.raises(OSError, match='not a regular file'):
                await call(name, '/work/pipe', 'x')
        with pytest.raises(OSError):
            await call('Path.read_text', '/work/pipe')
