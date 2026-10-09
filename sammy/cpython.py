"""The CPython tier (#6): real Python with packages (pandas, pypdf) for work Monty cannot do, run in a bubblewrap jail
on our own server, with the user's files.

```
agent: run_python(code)                                   one DBOS step (sammy.cpython.run_python)
  jail_of(workspaces, user_id)                            a BubblewrapWorkspace on the user's directory
    jail.run([CPYTHON, '-I', '-c', LIMITS, code])         a new jail for this call, gone when it returns
  what it printed -> the model
```

Each call gets a new jail, and the jail ends with the call: its processes are in their own PID namespace, which ends
when the command exits, and its `/tmp` is a fresh tmpfs. What stays is what the code wrote to the user's files.

What the jail sees:

- **Files:** the system's programs and libraries (`/usr`, `/bin`, `/lib`, `/etc`), read-only, and the user's directory,
  writable, as its working directory. Every other top-level folder (`/home`, `/root`, `/data`, `/app`, `/opt`,
  `/var`...), the folder of all users' files and the app's own folder are empty and read-only, wherever they are, so
  neither other users' files nor the app's (its settings file, its sign-ins) are there. `/tmp` is empty and small.
- **Environment:** `PATH`, `LANG` and a `HOME` in `/tmp`; nothing of the app's. `/proc` shows only the jail's own
  processes, so the app's environment cannot be read there either.
- **Network:** none (`BubblewrapWorkspace(network=False)`: its own network namespace, and a seccomp filter against
  sockets to other processes).
- **Limits:** CPU seconds, address space, file size and processes (`setrlimit`, hard, before the code runs; the jail
  has no capabilities to raise them), a small `/tmp`, and a wall-clock timeout that kills it. These are per process;
  a cgroup per call would cap the whole call's memory and CPU, which the server's container does not offer yet.
- **The user's files:** the call holds the user's lock (`Workspaces.lock`), so Monty's file calls and downloads wait
  until the jail is gone and never meet a link it is swapping.

Linux with `bwrap` only. Elsewhere `run_python` says it is not available, and the tests that need it are skipped.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.workspaces import WorkspaceError, WorkspaceOutputLimitError, WorkspaceTimeoutError
from pydantic_ai_harness.bubblewrap_sandbox import BubblewrapWorkspace

from sammy.deps import RunDeps
from sammy.resources import Resources, current
from sammy.settings import Settings
from sammy.workspaces import Workspaces

OUTPUT_LIMIT = 20_000
FILE_LIMIT = 100 << 20
"""The largest file the code may write, in bytes."""
TMP_BYTES = 256 << 20
"""The size of the jail's `/tmp`, which is memory."""
PROCESSES = 32
"""Processes and threads the code may have at once, so its memory is at most this many times `cpython_memory_mb`."""
CODE_LIMIT = 100_000
"""UTF-8 bytes of code per call: it travels on the command line, which caps one argument at 128 KiB."""

KEPT = frozenset({'usr', 'bin', 'sbin', 'lib', 'lib32', 'lib64', 'libx32', 'etc', 'proc', 'dev', 'sys', 'tmp', 'run'})
"""Top-level folders the jail sees: programs, libraries and their configuration. `/proc`, `/dev`, `/tmp` and `/run`
are the jail's own."""

LIMITS = """\
import resource, sys
limits = (
    (resource.RLIMIT_CPU, {cpu}),
    (resource.RLIMIT_AS, {memory}),
    (resource.RLIMIT_FSIZE, {size}),
    (resource.RLIMIT_NPROC, {processes}),
)
for which, value in limits:
    resource.setrlimit(which, (value, value))
code = sys.argv.pop(1)
sys.argv[0] = 'run_python'
exec(compile(code, '<run_python>', 'exec'), {{'__name__': '__main__'}})
"""
"""Runs before the agent's code, in the same process: hard limits, then the code from `argv[1]`."""

INSTRUCTIONS = """\
`run_python` runs real CPython with pandas and pypdf, for work the `run_code` sandbox cannot do: data frames, reading
PDFs. Each call starts fresh (no variables kept, no browser, no network) in the user's files: open them by relative
path, such as `downloads/invoice.csv` for `/work/downloads/invoice.csv`. Write results to a file there to use them
from `run_code`. `print` what you need to see."""


def can_jail() -> bool:
    return sys.platform == 'linux' and shutil.which('bwrap') is not None


def hidden_folders(workspaces_root: Path) -> list[str]:
    """The folders to cover with an empty, read-only tmpfs, parents first: every top-level folder but the ones in
    `KEPT` (and where a top-level link points), and wherever the users' files and the app's own folder are, even if
    that is under a kept one."""
    folders = {os.path.realpath(f'/{name}') for name in os.listdir('/') if name not in KEPT}
    folders |= {str(workspaces_root.resolve()), os.getcwd()}
    kept = {'/', *(f'/{name}' for name in KEPT)}
    return sorted(
        (f for f in folders if f not in kept and os.path.isdir(f) and not f.startswith(('/proc/', '/dev/', '/sys/'))),
        key=lambda f: (f.count('/'), f),
    )


def jail_of(workspaces: Workspaces, user_id: str) -> BubblewrapWorkspace:
    """A jail on the user's directory. Its `run` starts a new one for each command."""
    workspace = workspaces.of(user_id)
    directory = str(workspaces.directory(user_id).resolve())
    hidden = [f for f in hidden_folders(workspaces.root) if not (f + '/').startswith(directory + '/')]
    # Mounts apply in order: a small /tmp, empty folders over the hidden ones, the user's directory on top, then the
    # empty folders made read-only, so nothing can be written but the user's files and /tmp.
    args = ['--unshare-pid', '--size', str(TMP_BYTES), '--tmpfs', '/tmp']
    args += [arg for folder in hidden for arg in ('--tmpfs', folder)]
    args += ['--bind', directory, directory]
    args += [arg for folder in [*hidden, '/run', '/dev'] for arg in ('--remount-ro', folder)]
    return BubblewrapWorkspace(workspace, network=False, bwrap_args=args)


def shown(stdout: str, stderr: str, exit_code: int | None) -> str:
    text = stdout + (f'\n{stderr}' if stderr.strip() else '')
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + f'\n[{len(text) - OUTPUT_LIMIT} more characters of output cut]'
    if exit_code:
        text += f'\nExit code {exit_code}'
    return text.strip() or '(no output; print what you want to see)'


async def run_jailed(resources: Resources, user_id: str, code: str) -> str:
    """Run `code` in a new jail on the user's files; returns what to show the model."""
    settings: Settings = resources.settings
    if not can_jail():
        return 'Error: Python with packages is not available on this server; use `run_code`.'
    if len(code.encode('utf-8')) > CODE_LIMIT:
        return f'Error: the code is over {CODE_LIMIT} UTF-8 bytes; write it in smaller steps.'
    limits = LIMITS.format(
        cpu=settings.cpython_cpu_seconds, memory=settings.cpython_memory_mb << 20, size=FILE_LIMIT, processes=PROCESSES
    )
    jail = jail_of(resources.workspaces, user_id)
    try:
        async with resources.workspaces.lock(user_id):  # no file call of the user's runs meanwhile
            result = await jail.run(
                [settings.cpython, '-I', '-c', limits, code],
                env={'HOME': '/tmp', 'OPENBLAS_NUM_THREADS': '1'},
                timeout=settings.cpython_timeout_seconds,
            )
    except WorkspaceOutputLimitError as error:
        return shown(error.stdout, error.stderr, None) + '\nError: too much output; write it to a file and print less'
    except WorkspaceTimeoutError as error:
        return (
            shown(error.stdout, error.stderr, None)
            + f'\nError: the code ran over {settings.cpython_timeout_seconds:g} s'
        )
    except WorkspaceError:
        return 'Error: the Python sandbox could not start; try again, or use `run_code`.'
    return shown(result.stdout, result.stderr, result.exit_code)


cpython_tools: FunctionToolset[RunDeps] = FunctionToolset(id='cpython')


@cpython_tools.tool
async def run_python(ctx: RunContext[RunDeps], code: str) -> str:
    """Run Python 3 in real CPython with pandas and pypdf, in the user's files (see the instructions). Returns what it
    printed, and the exit code if it failed."""
    return await DBOS.run_step_async({'name': 'cpython.run'}, run_jailed, current(), ctx.deps.user_id, code)
