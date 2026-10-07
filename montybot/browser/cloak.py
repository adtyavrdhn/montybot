"""CloakBrowser (github.com/CloakHQ/CloakBrowser) in place of Chrome, under `ChromiumCDPBackend`. See `cloak.md` next to
this file for the evaluation and the license.

CloakBrowser is a Chromium with fingerprint patches in its C++ source: canvas, WebGL, audio, fonts, GPU, screen,
WebRTC and automation signals. It speaks the same CDP, so the backend, the pipe, the jail and the egress proxy stay as
they are. Only the binary and a few launch flags change. Neither Playwright nor CloakHQ's Python wrapper is used.

- **The binary** is at `$MONTYBOT_CLOAK_BINARY`. Its license forbids shipping it, so it is never in the repository or
  in an image we publish: CI and the evaluation download a free release from GitHub, pinned by SHA-256
  (`tests/linux/fetch_cloak.sh`).
- **The identity** comes from `Fingerprint.seed`: the same seed gives the same device (GPU, screen, canvas and audio
  noise) on every launch. The binary derives everything else from it.
- **Flags we drop:** `--enable-unsafe-swiftshader`, whose renderer string no real user has; CloakBrowser reports a
  GPU of its own. Headed, `--ignore-gpu-blocklist` lets WebGL run on a screen with no GPU (Xvfb), as their wrapper
  does.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from montybot.browser.cdp import CDPOptions

BINARY_ENV = 'MONTYBOT_CLOAK_BINARY'
SEED_ENV = 'MONTYBOT_CLOAK_SEED'
Platform = Literal['windows', 'macos', 'linux']


def cloak_executable() -> Path | None:
    """`$MONTYBOT_CLOAK_BINARY`, or None when it is not set."""
    value = os.environ.get(BINARY_ENV)
    return Path(value) if value else None


def fingerprint_seed(identity: str) -> int:
    """A `--fingerprint` seed for `identity`, stable across launches and machines. In 10000 to 99999, the range
    their wrapper picks from."""
    digest = hashlib.sha256(identity.encode()).digest()
    return 10000 + int.from_bytes(digest[:8], 'big') % 90000


def _default_seed() -> int:
    """`$MONTYBOT_CLOAK_SEED` if it is a number, else one derived from its text, or from `montybot`."""
    value = os.environ.get(SEED_ENV, '')
    return int(value) if value.isdigit() else fingerprint_seed(value or 'montybot')


def _default_platform() -> Platform:
    # A Mac build passes for a Mac only: its fonts and GPU are a Mac's. On Linux, Windows, as their wrapper does.
    return 'macos' if sys.platform == 'darwin' else 'windows'


@dataclass(frozen=True, kw_only=True)
class Fingerprint:
    """The device CloakBrowser presents. The binary makes the rest (GPU, screen, memory, noise) from `seed`."""

    seed: int = field(default_factory=_default_seed)
    """The same seed is the same device on every launch, so a site sees a returning visitor."""
    platform: Platform = field(default_factory=_default_platform)
    timezone: str = 'America/Chicago'
    """The server's egress address is in Iowa (GCP us-central1)."""
    locale: str = 'en-US'

    def args(self, *, headless: bool) -> tuple[str, ...]:
        """CloakBrowser's flags for this device."""
        args = (
            f'--fingerprint={self.seed}',
            f'--fingerprint-platform={self.platform}',
            f'--fingerprint-timezone={self.timezone}',
            # Chrome's own locale, not the jail's (LANG=C.UTF-8), and `navigator.languages` as a stock en-US Chrome
            # has it: ["en-US", "en"]. Not their `--fingerprint-locale`, which leaves only ["en-US"].
            f'--lang={self.locale}',
            f'--accept-lang={self.locale},{self.locale.split("-")[0]}',
        )
        return args if headless else (*args, '--ignore-gpu-blocklist')


def with_cloak(
    options: CDPOptions, *, executable: Path | None = None, fingerprint: Fingerprint | None = None
) -> CDPOptions:
    """`options` with CloakBrowser in place of Chrome: its binary (default `$MONTYBOT_CLOAK_BINARY`), its flags, and
    no SwiftShader. Everything else (jail, screen, proxy, the backend's own flags) stays."""
    executable = executable or cloak_executable()
    if executable is None:
        raise RuntimeError(f'CloakBrowser needs its binary: set {BINARY_ENV} (see montybot/browser/cloak.md)')
    fingerprint = fingerprint or Fingerprint()
    return dataclasses.replace(
        options,
        executable=executable,
        software_webgl=False,
        extra_args=(*fingerprint.args(headless=options.headless), *options.extra_args),
    )
