# Copied from viktor c1896df (viktor/settings.py). Slack, pgtask, GitHub, voice and sandbox settings removed;
# the model and the browser backend are chosen by name.
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', populate_by_name=True)

    database_url: str
    """Postgres for our tables (schema `montybot`) and DBOS's (schema `dbos`)."""
    host: str = '127.0.0.1'
    port: int = 8000
    public_url: str = 'http://127.0.0.1:8000'
    service_name: str = 'montybot'
    executor_id: str = 'local'
    """DBOS recovers the unfinished workflows of this executor when the app starts. One id per app process."""
    logfire_token: SecretStr | None = Field(default=None, alias='LOGFIRE_TOKEN')

    session_secret: SecretStr
    encryption_key: SecretStr
    """Encrypts each user's data key, which encrypts their saved sign-ins: 32 bytes, base64url
    (`python -c "from montybot.crypto import new_key; print(new_key())"`)."""
    """Signs the web app's session cookie."""
    secure_cookies: bool = False
    """Mark the session cookie `Secure`; on behind TLS."""

    model: str = 'claude-code:claude-opus-5-5'
    """A Pydantic AI model name; `claude-code:NAME` for a Claude Code subscription model (sign in with
    `montybot claude-code-login`); or `script:module:attribute` for a `Model` object, which is how tests script the
    model."""
    browser_backend: str = 'montybot.browser.fake:FakeBrowser'
    """`module:attribute` of a callable that makes a closed `BrowserBackend` for one run."""
    browser_idle_timeout_seconds: float = 10 * 60
    monty_url: str | None = None
    """Full Monty: monty-server's WebSocket URL, such as `ws://monty-server:8000`. Unset: Monty in local
    subprocesses."""
    code_timeout_seconds: float = 600
    """The longest one `run_code` call may take, browser calls included."""
    code_compute_seconds: float = 60
    """The longest one `run_code` call may compute, not counting time waiting on the browser."""
    workspaces_dir: Path = Path('data/workspaces')
    """Each user's files, one directory per user, which code sees at `/work` and browser downloads go to."""
    cpython: str = '/usr/bin/python3'
    """The CPython `run_python` runs in its jail (#6), with pandas and pypdf installed. Outside `/usr`, `/bin` and
    `/lib` the jail cannot see it."""
    cpython_timeout_seconds: float = 120
    cpython_cpu_seconds: int = 60
    cpython_memory_mb: int = 2048
    """Address space for one `run_python` call. numpy and pandas reserve far more than they use, so not too low."""
    allow_private_networks: bool = False
    """Let the agent open loopback and private addresses. Only for local fixture sites in tests."""

    vapid_private_key: SecretStr | None = None
    """Web push: the private key from `montybot keys`. Unset: no push notifications."""
    vapid_public_key: str | None = None
    vapid_subject: str = 'mailto:montybot@example.com'
    smtp_url: str | None = None
    """Email: `smtp://user:password@host:25`, `smtp+starttls://...:587` or `smtps://...:465`. Unset: no email."""
    mail_from: str = 'monty-bot <montybot@example.com>'

    ask_timeout_seconds: float = 24 * 60 * 60
    """How long a run waits for the user to answer a question, an approval or a hand-off."""
    history_limit: int = 40

    @field_validator('public_url')
    @classmethod
    def canonical_public_url(cls, value: str) -> str:
        return value.rstrip('/')

    @classmethod
    def from_environment(cls) -> Settings:
        return cls()  # pyright: ignore[reportCallIssue]  # required fields come from the environment
