"""The Linux launch path for `ChromiumBackend`: a virtual screen per browser, and Chrome inside bubblewrap.

On a Mac, the tests drive this code with stand-in `Xvfb` and `bwrap` scripts; on Linux (the app image) they run the
real ones.

- `start_virtual_screen` starts one Xvfb per browser. Xvfb picks a free display number itself (`-displayfd`), listens
  only on its socket file (no TCP, no abstract socket), and accepts only clients with this screen's random cookie.
- `write_bwrap_script` writes the program Playwright runs instead of Chrome. It starts Chrome inside bwrap with its
  own profile folder, a private `/tmp`, an empty environment, and only this screen's X socket. Playwright still talks
  to Chrome over the CDP pipe on fds 3 and 4, which bwrap passes through. There is no debugging port.

With a `proxy` socket, Chrome also gets its own network namespace, whose only way out is an `EgressProxy` that
refuses private addresses (`egress.py`). This replaces DESIGN.md's `pasta`: no extra program on the host, and the
address check sits in one place we can test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shlex
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from montybot.browser.egress import PROXY_PORT

X11_SOCKET_DIR = Path('/tmp/.X11-unix')
_FAMILY_WILD = 0xFFFF
_COOKIE_NAME = b'MIT-MAGIC-COOKIE-1'

# Read-only host paths Chrome needs inside the sandbox; missing ones are skipped. Not checked on a real distro yet.
_READ_ONLY_PATHS = (
    '/usr',
    '/etc/fonts',
    '/etc/ssl',
    '/etc/ca-certificates',
    '/etc/pki',
    '/etc/resolv.conf',
    '/etc/hosts',
    '/etc/nsswitch.conf',
    '/etc/localtime',
    '/sys/devices/system/cpu',
)
# Top-level folders that are symlinks into /usr on merged-/usr distros, and real folders on others.
_ROOT_LINKS = ('/bin', '/sbin', '/lib', '/lib32', '/lib64')


@dataclass(frozen=True, kw_only=True)
class Display:
    """An X display that one browser may use."""

    number: int
    xauthority: Path
    """The client cookie file. Only this display's browser gets it."""

    @property
    def name(self) -> str:
        """The `DISPLAY` value, such as `:7`."""
        return f':{self.number}'

    @property
    def socket(self) -> Path:
        return X11_SOCKET_DIR / f'X{self.number}'


@dataclass(kw_only=True)
class VirtualScreen:
    """A running Xvfb, owned by one browser."""

    display: Display
    process: asyncio.subprocess.Process

    async def stop(self) -> None:
        """Stop Xvfb: SIGTERM, then SIGKILL after 5 seconds. Safe to call twice."""
        if self.process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()


def xauthority_entry(*, number: str, cookie: bytes) -> bytes:
    """One `.Xauthority` record for any host (FamilyWild), display `number`, with an MIT-MAGIC-COOKIE-1."""

    def counted(data: bytes) -> bytes:
        return struct.pack('>H', len(data)) + data

    return (
        struct.pack('>H', _FAMILY_WILD)
        + counted(b'')
        + counted(number.encode())
        + counted(_COOKIE_NAME)
        + counted(cookie)
    )


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)


async def start_virtual_screen(
    *, workdir: Path, width: int, height: int, xvfb: str = 'Xvfb', timeout: float = 10
) -> VirtualScreen:
    """Start an Xvfb screen of `width` x `height` on a free display, with its files in `workdir`."""
    cookie = secrets.token_bytes(16)
    server_auth = workdir / 'xvfb-auth'
    _write_private(server_auth, xauthority_entry(number='', cookie=cookie))  # the server ignores the number
    read_fd, write_fd = os.pipe()
    args = [
        *('-displayfd', str(write_fd)),
        *('-screen', '0', f'{width}x{height}x24'),
        *('-nolisten', 'tcp'),
        *('-nolisten', 'local'),  # the Linux abstract socket, reachable from any process in the network namespace
        *('-auth', str(server_auth)),
        '-noreset',
    ]
    log = os.open(workdir / 'xvfb.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        process = await asyncio.create_subprocess_exec(
            xvfb,
            *args,
            pass_fds=(write_fd,),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
        os.close(log)
    try:
        line = await asyncio.wait_for(_read_line(read_fd), timeout)
        number = int(line)
    except (TimeoutError, ValueError) as error:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        log_tail = (workdir / 'xvfb.log').read_text(errors='replace')[-500:]
        raise RuntimeError(f'Xvfb did not start: {log_tail or "no output"}') from error
    xauthority = workdir / 'Xauthority'
    _write_private(xauthority, xauthority_entry(number=str(number), cookie=cookie))
    return VirtualScreen(display=Display(number=number, xauthority=xauthority), process=process)


async def _read_line(fd: int) -> bytes:
    """Read one line from the pipe `fd`, then close it."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, 'rb', 0))
    try:
        return await reader.readline()
    finally:
        transport.close()


def bwrap_command(
    *,
    chrome: Path,
    profile: Path,
    display: Display | None,
    proxy: Path | None = None,
    proxy_directory: Path | None = None,
    expose: tuple[int, Path] | None = None,
    env: Mapping[str, str] | None = None,
    read_only: Sequence[Path] = (),
    bwrap: str = 'bwrap',
) -> list[str]:
    """The bwrap command line that runs `chrome` (or another browser, such as servoshell) with `profile` as its only
    writable folder. `env` adds to the few variables the jail sets.

    Chrome's own arguments, which Playwright passes (`--user-data-dir=PROFILE`, `--remote-debugging-pipe`, ...),
    go after this. The profile is mounted at the same path inside, so Playwright's `--user-data-dir` works unchanged.

    With `proxy`, the Unix socket of the browser's `EgressProxy`, Chrome gets its own network namespace with only
    loopback, and socat inside forwards `127.0.0.1:PROXY_PORT` to the socket (see `egress.py`).

    `expose=(port, socket)`, with `proxy`, makes a TCP port inside the jail reachable from the host only as the Unix
    socket `socket`, which must be in `profile`: how Servo's WebDriver server, which listens on every interface, is
    driven from outside without a TCP port on the host.

    `read_only` mounts more host folders read-only at the same path, such as Camoufox's font cache.
    """
    command = [
        bwrap,
        '--unshare-user',
        '--unshare-pid',
        '--unshare-ipc',
        '--unshare-uts',
        '--unshare-cgroup-try',
        *(['--unshare-net'] if proxy is not None else []),
        '--die-with-parent',
        '--new-session',
        '--clearenv',
    ]
    for path in _READ_ONLY_PATHS:
        command += ['--ro-bind-try', path, path]
    for path in _ROOT_LINKS:
        if os.path.islink(path):
            command += ['--symlink', os.readlink(path), path]
        else:
            command += ['--ro-bind-try', path, path]
    chrome_dir = str(chrome.parent)
    command += ['--ro-bind', chrome_dir, chrome_dir]
    for path in read_only:
        command += ['--ro-bind', str(path), str(path)]
    command += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/dev/shm', '--tmpfs', '/tmp']
    command += ['--bind', str(profile), str(profile), '--chdir', str(profile)]
    variables = {'HOME': str(profile), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **(env or {})}
    if display is not None:
        command += ['--bind', str(display.socket), str(display.socket)]
        command += ['--ro-bind', str(display.xauthority), str(display.xauthority)]
        variables |= {'DISPLAY': display.name, 'XAUTHORITY': str(display.xauthority)}
    for name, value in variables.items():
        command += ['--setenv', name, value]
    if proxy is None:
        if expose is not None:
            raise ValueError('expose needs a proxy: without its own network namespace the port is already on the host')
        return [*command, str(chrome)]
    # A shared proxy may restart: bind its directory so socat sees the new socket inode on its next connection.
    if proxy_directory is not None:
        command += ['--ro-bind', str(proxy_directory), str(proxy_directory)]
    else:
        command += ['--bind', str(proxy), str(proxy)]
    script = _FORWARD_THEN_EXEC.format(port=PROXY_PORT, socket=shlex.quote(str(proxy)))
    if expose is not None:
        port, socket = expose
        script = _EXPOSE.format(port=port, socket=shlex.quote(str(socket))) + script
    return [*command, '/bin/sh', '-c', script, str(chrome)]


# Inside the jail: start the forwarder, wait until it listens (state 0A in /proc/net/tcp, for at most 5 s), then become
# Chrome ("$0") with Playwright's arguments, so the CDP pipe on fds 3 and 4 reaches it.
_FORWARD_THEN_EXEC = (
    'socat TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:{socket} 3>&- 4>&- & '
    'i=0; until grep -q " 0100007F:{port:04X} 00000000:0000 0A" /proc/net/tcp; do '
    'i=$((i+1)); [ $i -gt 500 ] && echo "socat did not start" >&2 && exit 1; sleep 0.01; done; '
    'exec "$0" "$@"'
)
# Inside the jail: the host connects to the Unix socket (mode 600, in the profile folder) to reach PORT on loopback.
_EXPOSE = 'socat UNIX-LISTEN:{socket},mode=600,unlink-early,fork TCP:127.0.0.1:{port} 3>&- 4>&- & '


def write_bwrap_script(
    *,
    path: Path,
    chrome: Path,
    profile: Path,
    display: Display | None,
    proxy: Path | None = None,
    proxy_directory: Path | None = None,
    bwrap: str = 'bwrap',
) -> Path:
    """Write an executable script at `path` that runs Chrome in bwrap with the arguments it is given."""
    command = bwrap_command(
        chrome=chrome, profile=profile, display=display, proxy=proxy, proxy_directory=proxy_directory, bwrap=bwrap
    )
    path.write_text(
        f'#!/bin/sh\n# Written by montybot for one browser. CDP stays on fds 3 and 4.\nexec {shlex.join(command)} "$@"\n'
    )
    path.chmod(0o700)
    return path
