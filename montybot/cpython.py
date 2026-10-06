"""The CPython tier (#6): real Python with packages (pandas, pypdf) for work Monty cannot do, run in a bubblewrap jail
on our own server, with the user's files.

```
agent: run_python(code)                                   one DBOS step (montybot.cpython.run_python)
  jail_of(workspaces, user_id)                            a BubblewrapWorkspace on the user's directory
    jail.run([CPYTHON, '-I', '-c', LIMITS, code])         a new jail for this call, gone when it returns
  what it printed -> the model
```

Each call gets a new jail, and the jail ends with the call: its processes are in their own PID namespace, which ends
when the command exits, and its `/tmp` is a fresh tmpfs. What stays is what the code wrote to the user's files.

What the jail sees:

- **Files:** the system's programs and libraries (`/usr`, `/bin`, `/lib`, `/etc`), read-only, and the user's directory,
  writable, as its working directory. Every other top-level folder (`/home`, `/root`, `/data`, `/app`, `/opt`,
  `/var`...) is an empty tmpfs, so neither other users' files nor the app's own (its settings file, its sign-ins) are
  there. `/tmp` and `/run` are empty too.
- **Environment:** `PATH`, `LANG` and a `HOME` in `/tmp`; nothing of the app's. `/proc` shows only the jail's own
  processes, so the app's environment cannot be read there either.
- **Network:** none (`BubblewrapWorkspace(network=False)`: its own network namespace, and a seccomp filter against
  sockets to other processes).
- **Limits:** CPU seconds, address space and file size (`setrlimit`, hard, before the code runs; the jail has no
  capabilities to raise them), and a wall-clock timeout that kills it.

Linux with `bwrap` only. Elsewhere `run_python` says it is not available, and the tests that need it are skipped.
"""

from __future__ import annotations

import os
import shutil
import sys

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.workspaces import WorkspaceError, WorkspaceTimeoutError
from pydantic_ai_harness.bubblewrap_sandbox import BubblewrapWorkspace

from montybot.deps import RunDeps
from montybot.resources import Resources, current
from montybot.settings import Settings
from montybot.workspaces import Workspaces

OUTPUT_LIMIT = 20_000
FILE_LIMIT = 100 << 20
"""The largest file the code may write, in bytes."""
CODE_LIMIT = 100_000
"""Characters of code per call: it travels on the command line, which caps one argument at 128 KiB."""

KEPT = frozenset({'usr', 'bin', 'sbin', 'lib', 'lib32', 'lib64', 'libx32', 'etc', 'proc', 'dev', 'sys', 'tmp', 'run'})
"""Top-level folders the jail sees: programs, libraries and their configuration. `/proc`, `/dev`, `/tmp` and `/run`
are the jail's own."""

LIMITS = """\
import resource, sys
for which, value in ((resource.RLIMIT_CPU, {cpu}), (resource.RLIMIT_AS, {memory}), (resource.RLIMIT_FSIZE, {size})):
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


def hidden_folders() -> list[str]:
    """Every top-level folder but the ones in `KEPT`, to cover with an empty tmpfs."""
    return sorted(
        path
        for name in os.listdir('/')
        if name not in KEPT and os.path.isdir(path := f'/{name}') and not os.path.islink(path)
    )


def jail_of(workspaces: Workspaces, user_id: str) -> BubblewrapWorkspace:
    """A jail on the user's directory. Its `run` starts a new one for each command."""
    workspace = workspaces.of(user_id)
    directory = str(workspaces.directory(user_id).resolve())
    hide = [arg for folder in hidden_folders() for arg in ('--tmpfs', folder)]
    # Mounts apply in order: hide the folders, then bring the user's directory back on top.
    return BubblewrapWorkspace(
        workspace, network=False, bwrap_args=['--unshare-pid', *hide, '--bind', directory, directory]
    )


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
    if len(code) > CODE_LIMIT:
        return f'Error: the code is over {CODE_LIMIT} characters; write it in smaller steps.'
    limits = LIMITS.format(cpu=settings.cpython_cpu_seconds, memory=settings.cpython_memory_mb << 20, size=FILE_LIMIT)
    jail = jail_of(resources.workspaces, user_id)
    try:
        result = await jail.run(
            [settings.cpython, '-I', '-c', limits, code],
            env={'HOME': '/tmp', 'OPENBLAS_NUM_THREADS': '1'},
            timeout=settings.cpython_timeout_seconds,
        )
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
