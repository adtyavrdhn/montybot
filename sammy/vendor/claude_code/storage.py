"""Token storage for Claude Code credentials.

By default credentials live in the OS keyring: the Keychain on macOS, Credential
Manager on Windows, Secret Service on Linux. When no keychain is available, a
JSON file readable only by you (`0600`) is used instead. Force a backend with the
`CLAUDE_CODE_CREDENTIALS` env var (`keyring` or `file`), or pass it to `default_store`;
the file path honors `CLAUDE_CODE_AUTH_FILE`.

A named profile keeps a second account apart: keyring user name `PROFILE`, or the
file `auth-PROFILE.json` next to the default one. No profile is the default account.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal, Protocol, get_args

from pydantic_ai.exceptions import UserError

from .credentials import ClaudeCodeCredentials

_FILE_PERMISSIONS = 0o600
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

Backend = Literal["auto", "keyring", "file"]
"""Where credentials live: `auto` uses the keyring when one exists, else the file."""


class TokenStore(Protocol):
    """Sibling store backends must both implement this shape."""

    def load(self) -> ClaudeCodeCredentials | None:
        """Return stored credentials, or `None` when nothing is stored yet."""
        ...

    def save(self, credentials: ClaudeCodeCredentials) -> None:
        """Persist credentials for the next run."""
        ...

    def delete(self) -> None:
        """Forget stored credentials; a no-op when nothing is stored."""
        ...


def check_profile(profile: str) -> str:
    """Return `profile` if it is a valid profile name, else raise `UserError`."""
    if not _PROFILE.fullmatch(profile) or profile == KeyringTokenStore.USERNAME:
        raise UserError(
            f"Profile {profile!r} must be 1 to 32 lowercase letters, digits, hyphens, or underscores, starting with "
            "a letter or digit, and not 'default' (the account without a profile)."
        )
    return profile


def default_auth_path(profile: str | None = None) -> Path:
    """The fallback token file path, honoring `CLAUDE_CODE_AUTH_FILE`; a profile's file sits next to it."""
    env_path = os.environ.get("CLAUDE_CODE_AUTH_FILE")
    if env_path:
        path = Path(env_path).expanduser()
    else:
        data_dir = Path(os.environ.get("XDG_DATA_HOME", "") or (Path.home() / ".local" / "share"))
        path = data_dir / "pydantic-ai-claude-code" / "auth.json"
    return path if profile is None else path.with_name(f"auth-{check_profile(profile)}.json")


def refresh_lock_path() -> Path:
    """The lock file every process takes to refresh a sign-in, next to the default token file."""
    return default_auth_path().with_name("refresh.lock")


def default_store(backend: Backend | None = None, profile: str | None = None) -> TokenStore:
    """The store for `backend` and `profile`; `backend` defaults to `CLAUDE_CODE_CREDENTIALS`, then `auto`."""
    if backend is None:
        backend = _env_backend()
    if backend == "file" or (backend == "auto" and not _keyring_available()):
        return ClaudeCodeTokenStore(default_auth_path(profile))
    return KeyringTokenStore(username=KeyringTokenStore.USERNAME if profile is None else check_profile(profile))


def _env_backend() -> Backend:
    value = os.environ.get("CLAUDE_CODE_CREDENTIALS", "auto").lower()
    for backend in get_args(Backend):
        if value == backend:
            return backend
    raise UserError(f"Unknown CLAUDE_CODE_CREDENTIALS backend {value!r}; expected 'auto', 'keyring', or 'file'.")


class ClaudeCodeTokenStore:
    """JSON-file store readable only by you (`0600`), the no-keychain fallback."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_auth_path()

    def load(self) -> ClaudeCodeCredentials | None:
        """Return stored credentials, or `None` when nothing is stored yet."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise UserError(f"Unable to read Claude Code credentials from {str(self.path)!r}.") from exc
        return ClaudeCodeCredentials.from_token_file(data)

    def save(self, credentials: ClaudeCodeCredentials) -> None:
        """Persist credentials atomically, creating the file `0600` so the tokens are never world-readable."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_PERMISSIONS)
        with os.fdopen(fd, "w") as file:
            file.write(json.dumps(credentials.to_wire_dict(), indent=2))
        os.replace(tmp, self.path)

    def delete(self) -> None:
        """Remove the token file."""
        self.path.unlink(missing_ok=True)


class KeyringTokenStore:
    """OS-keyring store backed by the `keyring` package.

    The service name is `pydantic-ai-claude-code` and credentials are stored as
    a single JSON blob under the `default` user name, or the profile's name.
    """

    SERVICE = "pydantic-ai-claude-code"
    USERNAME = "default"

    def __init__(self, service: str = SERVICE, username: str = USERNAME) -> None:
        self.service = service
        self.username = username

    def load(self) -> ClaudeCodeCredentials | None:
        """Return stored credentials from the keyring, or `None` when absent."""
        raw = self._keyring_get()
        if not raw:
            return None
        return ClaudeCodeCredentials.from_token_file(json.loads(raw))

    def save(self, credentials: ClaudeCodeCredentials) -> None:
        """Persist credentials to the keyring."""
        self._keyring_require()
        import keyring

        keyring.set_password(self.service, self.username, json.dumps(credentials.to_wire_dict()))

    def delete(self) -> None:
        """Remove the keyring entry."""
        if self._keyring_get() is None:
            return
        import keyring

        keyring.delete_password(self.service, self.username)

    def _keyring_get(self) -> str | None:
        self._keyring_require()
        import keyring

        return keyring.get_password(self.service, self.username)

    @staticmethod
    def _keyring_require() -> None:
        if not _keyring_available():
            raise UserError("No OS keyring is available. Set CLAUDE_CODE_CREDENTIALS=file to use the JSON file store.")


def _keyring_available() -> bool:
    """Whether a real keychain backs `keyring`.

    Without one (headless Linux, CI), `keyring` does not raise: it returns its `fail` backend, which raises
    only on use, or the `null` backend when configured off. Neither can hold credentials.
    """
    try:
        import keyring
        from keyring.backends import fail, null

        return not isinstance(keyring.get_keyring(), fail.Keyring | null.Keyring)
    except Exception:  # noqa: BLE001 - a broken keyring install counts as no keyring
        return False
