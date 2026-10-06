"""Run servoshell on Linux inside bwrap, with its own network namespace from pasta. Issue #12.

**Not run on Linux yet.** This machine is a Mac; the command line below follows `DESIGN.md`'s Chromium script and
must be checked on the server from #7. Use it as `ServoOptions(launcher=BwrapLauncher(...))`.

What it builds, outside in:

    pasta --config-net --no-map-gw -t 127.0.0.1/PORT -u none -T none -U none --   own network namespace; only the
                                                                                    WebDriver port is forwarded in,
                                                                                    on the host's loopback
      bwrap --unshare-all --share-net --die-with-parent --new-session ...       own user, pid, IPC and UTS
                                                                                    namespaces; host files read-only;
                                                                                    private /tmp; the profile folder is
                                                                                    the only writable bind
        servoshell --headless --webdriver=PORT --config-dir=PROFILE ...

Why pasta: servoshell 0.7.0 binds its WebDriver server to `0.0.0.0`, not loopback
(`components/webdriver_server/lib.rs`). In its own network namespace that only exposes it inside the namespace, and
pasta forwards it to `127.0.0.1` on the host. Without pasta (`pasta=None`), Servo shares the host's network and its
WebDriver port listens on every interface, which is fine on a laptop behind a firewall and not on a server.

`close()` kills the whole process group with SIGKILL. pasta is the group leader; bwrap's `--die-with-parent` takes
Servo down with it, and `--unshare-pid` makes sure nothing it started survives.

Headless Servo renders in software, so unlike visible Chrome it needs no Xvfb. It still needs fonts and Mesa's
libraries from `/usr`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

READ_ONLY = (
    '/usr',
    '/etc/fonts',
    '/etc/ssl',
    '/etc/ca-certificates',
    '/etc/pki',
    '/etc/resolv.conf',
    '/etc/hosts',
    '/etc/nsswitch.conf',
    '/etc/localtime',
)
"""Host paths Servo may need, bound read-only when they exist (`--ro-bind-try`)."""


@dataclass(frozen=True, kw_only=True)
class BwrapLauncher:
    """A `Launcher` for `ServoOptions`: servoshell in bwrap, in pasta's network namespace."""

    bwrap: str = 'bwrap'
    pasta: str | None = 'pasta'
    """None shares the host's network. See the module docstring before doing that on a server."""
    read_only: tuple[str, ...] = READ_ONLY
    extra_read_only: tuple[str, ...] = ()
    """More read-only binds, such as a hosts file passed with `ServoOptions(host_file=...)`."""

    def __call__(self, argv: Sequence[str], *, config_dir: Path, port: int) -> list[str]:
        servo_dir = str(Path(argv[0]).parent)  # the release tarball's folder: servoshell and its resources
        sandbox = [
            self.bwrap,
            '--unshare-all',
            '--share-net',  # keep pasta's namespace, or no network without pasta
            '--die-with-parent',
            '--new-session',
            *(arg for path in (*self.read_only, *self.extra_read_only) for arg in ('--ro-bind-try', path, path)),
            '--symlink',
            'usr/lib',
            '/lib',
            '--symlink',
            'usr/lib64',
            '/lib64',
            '--symlink',
            'usr/bin',
            '/bin',
            '--ro-bind',
            servo_dir,
            servo_dir,
            '--bind',
            str(config_dir),
            str(config_dir),
            '--proc',
            '/proc',
            '--dev',
            '/dev',
            '--tmpfs',
            '/tmp',
            '--setenv',
            'HOME',
            str(config_dir),
            '--chdir',
            str(config_dir),
            '--',
            *argv,
        ]
        if self.pasta is None:
            return sandbox
        network = [
            self.pasta,
            '--config-net',
            '--quiet',
            '--no-map-gw',  # the host is not reachable through the gateway address
            '-t',
            f'127.0.0.1/{port}',  # host loopback PORT -> Servo's WebDriver in the namespace
            '-u',
            'none',
            '-T',
            'none',
            '-U',
            'none',
            '--',
        ]
        return [*network, *sandbox]
