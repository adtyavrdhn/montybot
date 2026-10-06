"""The Linux launch path (Xvfb per browser, Chrome in bwrap), run with stand-in `Xvfb` and `bwrap` scripts.

The stand-ins check the command lines and pass Chrome through unchanged, so on a Mac this proves the plumbing:
Playwright runs our wrapper script, the CDP pipe survives the `exec`, and everything started is stopped. The real
`bwrap` and `Xvfb` run only in `test_real_bwrap_and_xvfb`, which needs Linux.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.chromium_linux import (
    Display,
    bwrap_command,
    start_virtual_screen,
    write_bwrap_script,
    xauthority_entry,
)
from montybot.browser.conformance import Site, sample_state, serve_site
from montybot.browser.contract import Navigate

pytestmark = pytest.mark.anyio

# bash, not sh: Ubuntu's dash cannot redirect to a file descriptor above 9, and -displayfd is usually one.
FAKE_XVFB = """#!/bin/bash
echo "$@" > "$(dirname "$0")/xvfb-args"
while [ $# -gt 0 ]; do [ "$1" = -displayfd ] && fd=$2; shift; done
eval "echo 99 >&$fd"
exec sleep 600
"""

# Applies bwrap's environment options, checks every option's arity, then runs the program as it is.
FAKE_BWRAP = """#!{python}
import os, sys
ARITY = {{'--proc': 1, '--dev': 1, '--tmpfs': 1, '--chdir': 1, '--ro-bind': 2, '--ro-bind-try': 2, '--bind': 2,
          '--symlink': 2, '--setenv': 2}}
args, env = sys.argv[1:], dict(os.environ)
with open({log!r}, 'w') as log:
    log.write('\\n'.join(args))
while args[0].startswith('--'):
    option = args.pop(0)
    if option == '--clearenv':
        env = {{}}
    elif option == '--setenv':
        env[args[0]] = args[1]
    elif option not in ARITY and not option.startswith(('--unshare-', '--die-with-parent', '--new-session')):
        sys.exit(f'fake bwrap: unknown option {{option}}')
    del args[:ARITY.get(option, 0)]
os.execve(args[0], args, env)
"""


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def make_script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


def test_xauthority_entry_is_a_wildcard_cookie() -> None:
    entry = xauthority_entry(number='7', cookie=b'\x01' * 16)
    assert entry == (
        struct.pack('>H', 0xFFFF)
        + b'\x00\x00'
        + b'\x00\x017'
        + b'\x00\x12MIT-MAGIC-COOKIE-1'
        + b'\x00\x10'
        + b'\x01' * 16
    )


def test_bwrap_command(tmp_path: Path) -> None:
    profile = tmp_path / 'profile'
    display = Display(number=7, xauthority=tmp_path / 'Xauthority')
    command = bwrap_command(chrome=Path('/opt/chrome/chrome'), profile=profile, display=display)
    pairs = list(zip(command, command[1:], command[2:], strict=False))
    assert command[0] == 'bwrap' and command[-1] == '/opt/chrome/chrome'
    for flag in (
        '--unshare-user',
        '--unshare-pid',
        '--unshare-ipc',
        '--die-with-parent',
        '--new-session',
        '--clearenv',
    ):
        assert flag in command
    assert '--unshare-net' not in command  # the browser needs the internet; pasta's job (#7)
    assert ('--bind', str(profile), str(profile)) in pairs  # Playwright's --user-data-dir works unchanged
    assert ('--ro-bind', '/opt/chrome', '/opt/chrome') in pairs
    assert ('--tmpfs', '/tmp', '--bind') in pairs
    assert ('--bind', '/tmp/.X11-unix/X7', '/tmp/.X11-unix/X7') in pairs  # only this browser's screen
    assert ('--setenv', 'DISPLAY', ':7') in pairs
    assert ('--setenv', 'XAUTHORITY', str(tmp_path / 'Xauthority')) in pairs
    headless = bwrap_command(chrome=Path('/opt/chrome/chrome'), profile=profile, display=None)
    assert 'DISPLAY' not in headless


def test_bwrap_script_quotes_paths(tmp_path: Path) -> None:
    script = write_bwrap_script(
        path=tmp_path / 'chrome-in-bwrap', chrome=Path('/a b/Chrome'), profile=tmp_path / 'p', display=None
    )
    text = script.read_text()
    assert text.startswith('#!/bin/sh\n') and '\'/a b/Chrome\' "$@"\n' in text
    assert script.stat().st_mode & 0o777 == 0o700


async def test_virtual_screen_starts_and_stops(tmp_path: Path) -> None:
    xvfb = make_script(tmp_path / 'Xvfb', FAKE_XVFB)
    screen = await start_virtual_screen(workdir=tmp_path, width=1280, height=800, xvfb=xvfb)
    try:
        assert screen.display.name == ':99'
        assert screen.display.xauthority.read_bytes()[:8] == b'\xff\xff\x00\x00\x00\x0299'
        args = (tmp_path / 'xvfb-args').read_text().split()
        assert args[args.index('-screen') + 2] == '1280x800x24'
        assert '-nolisten' in args and 'tcp' in args and 'local' in args
        assert (tmp_path / 'xvfb-auth').stat().st_mode & 0o777 == 0o600
    finally:
        await screen.stop()
    assert screen.process.returncode is not None
    await screen.stop()


async def test_virtual_screen_that_fails_to_start(tmp_path: Path) -> None:
    xvfb = make_script(tmp_path / 'Xvfb', '#!/bin/sh\necho "no screens" >&2\nexit 1\n')
    with pytest.raises(RuntimeError, match='no screens'):
        await start_virtual_screen(workdir=tmp_path, width=1280, height=800, xvfb=xvfb)


@asynccontextmanager
async def server_backend(options: ChromiumOptions) -> AsyncGenerator[ChromiumBackend]:
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Playwright Chromium is not installed')
        backend = ChromiumBackend(playwright=playwright, options=options)
        try:
            yield backend
        finally:
            await backend.close()


async def check_server_launch(backend: ChromiumBackend, site: Site) -> Path:
    """Open a signed-in state, navigate, export, close; return the workdir, which close() deleted."""
    state = sample_state(site, site.probe)
    await backend.open(state)
    workdir = backend.workdir
    assert workdir is not None
    assert 'server saw: [sid theme]' in (await backend.snapshot()).text
    await backend.act(Navigate(url=site.home))
    exported = await backend.release()
    assert {c.name for c in exported.cookies} == {'sid', 'theme', 'other'}
    assert not workdir.exists()
    return workdir


@pytest.mark.skipif(sys.platform == 'linux', reason='headed Chrome under the stand-in Xvfb needs a real screen')
@pytest.mark.skipif(
    os.environ.get('MONTYBOT_HEADED') != '1', reason='opens a real window on this desktop; set MONTYBOT_HEADED=1 to run'
)
async def test_server_launch_with_stand_ins(tmp_path: Path) -> None:
    log = tmp_path / 'bwrap-args'
    options = ChromiumOptions.server(
        xvfb_path=make_script(tmp_path / 'Xvfb', FAKE_XVFB),
        bwrap_path=make_script(tmp_path / 'bwrap', FAKE_BWRAP.format(python=sys.executable, log=str(log))),
    )
    async with server_backend(options) as backend:
        workdir = await check_server_launch(backend, serve_site())
    args = log.read_text().splitlines()
    profile = str(workdir / 'profile')
    assert ['--bind', profile, profile] == args[args.index('--bind') : args.index('--bind') + 3]
    assert ['--setenv', 'DISPLAY', ':99'] == args[args.index('DISPLAY') - 1 : args.index('DISPLAY') + 2]
    assert '--remote-debugging-pipe' in args  # Playwright's own flag: CDP on fds 3 and 4, no port
    assert f'--user-data-dir={profile}' in args
    assert not any(a.startswith('--remote-debugging-port') for a in args)


@pytest.mark.skipif(
    sys.platform != 'linux' or not (shutil.which('bwrap') and shutil.which('Xvfb')), reason='needs Linux, bwrap, Xvfb'
)
async def test_real_bwrap_and_xvfb() -> None:
    async with server_backend(ChromiumOptions.server()) as backend:
        await check_server_launch(backend, serve_site())
