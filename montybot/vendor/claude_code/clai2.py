"""The CLAI2 plugin: chat with your Claude Code subscription as `claude-code:MODEL`.

Install it by copying this package's folder into CLAI2's plugins folder as `claude_code/`; CLAI2 loads it at
startup through the `ClaudeCodePlugin` the package's `__init__.py` declares. Everything it imports already ships with CLAI2, and it uses only
relative imports, so the copy runs on its own. `/login claude-code` signs in through the browser, and `/claude_code`
(or `C` in `/plugins`) opens the settings menu: sign in, sign out, and choose where the sign-in is kept. Then
pick a model in `/add_model` > `claude-code`.

The sign-in is an OAuth token pair, not an API key, so it does not go in `/keys`. It is kept where this
package keeps it outside CLAI2 (the OS keyring by default), so one sign-in serves CLAI2 and your own agents.
Plugin settings, which are plaintext SQLite, hold only the storage choice.

On a CLAI2 build with auth profiles, `/login claude-code@PROFILE` signs in another account, kept apart from the
default one, and `claude-code@PROFILE:MODEL` runs on it. A CLAI2 fallback chain can pool those accounts.
On a CLAI2 build with account usage, `/accounts` shows each account's five-hour and weekly usage.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import IO, Any, cast, get_args

from anthropic import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_clai2.commands import Command
from termflow.tui.terminal import raw_mode
from pydantic_clai2.plugins import ModelProvider, Plugin, PluginHost, PluginLogin, SessionEnd, SessionStart, TurnEnd
from pydantic_clai2.ui.menus.menu_worker import menu_key, run_worker
from pydantic_clai2.ui.menus.field_menu import TERMINAL, FieldMenu, FieldRow, Runners, first_error, run_flow_async

from . import __version__, config, updates
from .flow import login, parse_pasteback
from .model import ClaudeCodeModel
from .provider import ClaudeCodeProvider
from .storage import Backend, ClaudeCodeTokenStore, TokenStore, default_store

PREFIX = "claude-code"
"""Models run as `claude-code:NAME`; `NAME` is any Claude model ID the subscription serves."""

COMMAND = "claude_code"
"""CLAI2 command names are Python identifiers, so the command cannot share the prefix's hyphen."""

LOGIN = PREFIX
"""`/login claude-code` signs in, matching the `claude-code:` models it unlocks."""

SIGNED_IN = "signed in"
SIGNED_OUT = "not signed in"
_HELP = (
    f"Usage: /{COMMAND} (settings menu), /{COMMAND} logout [PROFILE], or /{COMMAND} status [PROFILE]. "
    f"Sign in with /login {LOGIN}."
)


def _login(profile: str | None) -> str:
    """The `/login` argument for a profile: `claude-code`, or `claude-code@PROFILE`."""
    return LOGIN if profile is None else f"{LOGIN}@{profile}"


def _account(profile: str | None) -> str:
    return "Claude Code" if profile is None else f"Claude Code profile {profile}"


def _supported(cls: type, **optional: Any) -> dict[str, Any]:
    """The keyword arguments `cls` accepts: CLAI2 builds without auth profiles lack those fields."""
    names = {field.name for field in dataclasses.fields(cls)}
    return {key: value for key, value in optional.items() if key in names}


class ClaudeCodeSettings(BaseModel):
    """The JSON a `claude_code` declaration may carry. Nothing here is secret."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    credentials: Backend = Field(
        default="auto",
        description="Where the sign-in is kept: auto follows CLAUDE_CODE_CREDENTIALS when set, else the OS "
        "keyring when one exists, else a file; or always the keyring; or always a file readable only by you.",
    )


def token_store(backend: Backend, profile: str | None = None) -> TokenStore:
    """The store for a plugin setting and profile; `auto` defers to `CLAUDE_CODE_CREDENTIALS`, as the package does."""
    return default_store(None if backend == "auto" else backend, profile)


class ClaudeCodeConfig:
    """The settings menu's rows: the sign-in, kept in the token store, and the storage choice, in plugin settings."""

    title = "Claude Code settings"

    def __init__(
        self,
        settings: ClaudeCodeSettings,
        save: Callable[[ClaudeCodeSettings], None],
    ) -> None:
        """`save` persists new settings, as `host.save_settings` does."""
        self.settings = settings
        self._save = save

    def store(self, profile: str | None = None) -> TokenStore:
        """The token store the current settings choose, for the default account or a profile."""
        return token_store(self.settings.credentials, profile)

    def rows(self) -> Sequence[FieldRow]:
        """The sign-in first, then where it is kept."""
        return (
            FieldRow(
                key="account",
                label="Sign-in",
                description="Enter signs in to your Claude subscription through the browser; R signs out.",
                default=SIGNED_OUT,
            ),
            FieldRow(
                key="credentials",
                label="Credential storage",
                description=ClaudeCodeSettings.model_fields["credentials"].description or "",
                default="auto",
                choices=get_args(Backend),
                choice_labels={"auto": "Keyring, else a file", "keyring": "OS keyring", "file": "File (0600)"},
                allow_custom=False,
            ),
        )

    def current(self, row: FieldRow) -> str:
        """Whether a sign-in is stored, or the storage choice. The menu runs this off the event loop."""
        if row.key == "account":
            return SIGNED_IN if self.store().load() is not None else SIGNED_OUT
        return self.settings.credentials

    def problem(self, row: FieldRow, text: str) -> str | None:
        """Only the storage row takes a value; the sign-in row opens the browser instead."""
        if row.key == "account":
            return None
        try:
            self._validated(text)
        except ValidationError as exc:
            return first_error(exc)
        return None

    def apply(self, row: FieldRow, raw: str) -> str:
        """Save the storage choice now; the next run reads the sign-in from there."""
        settings = self._validated(raw)
        self._save(settings)
        self.settings = settings
        return f"Claude Code credential storage: {row.display(raw)}. {status(self)}"

    def reset(self, row: FieldRow) -> str:
        """Sign out, or restore automatic storage."""
        if row.key == "account":
            return sign_out(self)
        return self.apply(row, "auto")

    def _validated(self, raw: str) -> ClaudeCodeSettings:
        return ClaudeCodeSettings.model_validate({**self.settings.model_dump(), "credentials": raw})


class Providers:
    """One provider per store and profile, reused across runs so they share a connection pool.

    A provider is rebuilt when the stored sign-in differs from the one it holds: after signing in again here
    or from another process. Refreshes the provider makes itself are saved back, so they keep it in step.
    """

    def __init__(self) -> None:
        """Start empty; a provider is built on the first run."""
        self._cached: dict[tuple[Backend, str | None], ClaudeCodeProvider] = {}

    def model(self, name: str, source: ClaudeCodeConfig, profile: str | None = None) -> ClaudeCodeModel:
        """Build `name` on the stored sign-in, raising `UserError` with setup steps when there is none."""
        return ClaudeCodeModel(name, provider=self.provider(source, profile))

    def provider(self, source: ClaudeCodeConfig, profile: str | None = None) -> ClaudeCodeProvider:
        """The provider for the stored sign-in, shared by models and usage checks."""
        key = (source.settings.credentials, profile)
        store = source.store(profile)
        credentials = store.load()
        if credentials is None:
            raise UserError(f"Sign in to Claude Code first: /login {_login(profile)}.")
        provider = self._cached.get(key)
        if provider is None or provider.credentials != credentials:
            provider = self._cached[key] = ClaudeCodeProvider(credentials, store=store)
        return provider


USAGE_PATH = "/api/oauth/usage"
"""What Claude Code's `/usage` reads. The provider's auth signs and refreshes the request like a model call."""

USAGE_WINDOWS = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d opus", "seven_day_sonnet": "7d sonnet"}
"""The windows CLAI2's `/accounts` shows, by the usage response's key; ones the plan lacks come back null."""


class _UsageWindow(BaseModel):
    utilization: float
    resets_at: datetime | None = None


def _usage_types() -> tuple[Any, Any]:
    """CLAI2's `AccountUsage` and `UsageWindow`; only builds that offer `PluginLogin.usage` ask for usage."""
    plugins = importlib.import_module("pydantic_clai2.plugins")
    return plugins.AccountUsage, plugins.UsageWindow


async def account_usage(provider: ClaudeCodeProvider) -> Any:
    """The subscription's five-hour and weekly windows, as CLAI2's `AccountUsage`."""
    try:
        body = await provider.client.with_options(max_retries=0, timeout=10).get(USAGE_PATH, cast_to=object)
    except APIError as exc:
        raise UserError(f"Claude Code usage unavailable: {exc.message}") from None
    if not isinstance(body, dict):
        raise UserError("Claude Code returned usage the plugin cannot read.")
    usage_type, window_type = _usage_types()
    windows: list[Any] = []
    for key, label in USAGE_WINDOWS.items():
        try:
            window = _UsageWindow.model_validate(cast("dict[str, object]", body).get(key))
        except ValidationError:
            continue  # absent, null, or a shape this version does not know
        windows.append(window_type(label=label, used_percent=window.utilization, resets_at=window.resets_at))
    return usage_type(windows=tuple(windows))


def status(source: ClaudeCodeConfig, profile: str | None = None) -> str:
    """One line on whether the plugin can run models, and where the sign-in lives."""
    store = source.store(profile)
    where = f"the file {store.path}" if isinstance(store, ClaudeCodeTokenStore) else "the OS keyring"
    if store.load() is None:
        to = "" if profile is None else f" to profile {profile}"
        return f"Not signed in{to} (sign-in would be kept in {where}). Run /login {_login(profile)}."
    return f"Signed in to {_account(profile)}; tokens are kept in {where}."


async def sign_in(source: ClaudeCodeConfig, show_url: Callable[[str], None], profile: str | None = None) -> str:
    """Sign in through the browser, or a pasted redirect address, and save the tokens to the chosen store."""
    store = source.store(profile)
    await login(store=store, on_url=show_url, read_pasteback=lambda: run_worker(read_pasted_url))
    first = config.MODELS[0]
    if profile is not None:
        return f"Signed in to {_account(profile)}. Run /model {PREFIX}@{profile}:{first}, or any other model."
    return f"Signed in to Claude Code. Choose a model in /add_model > {PREFIX}, or run /model {PREFIX}:{first}."


PASTE_PROMPT = "Or paste the address your browser ended on, then Enter (Esc cancels): "


def read_pasted_url(keys: Callable[[], str] = menu_key, output: IO[str] = sys.stdout) -> str | None:
    """Read a pasted redirect address on one line, under the printed sign-in URL; `None` on Esc.

    The address carries the authorization code, so it is counted, not echoed: a long URL would also wrap
    and break the one-line redraw. `keys` is `run_worker`'s `menu_key`, which turns into `ctrl-c` when the
    browser's callback wins and `login` cancels this read.
    """
    chars: list[str] = []
    note = ""

    def draw() -> None:
        shown = note or (f"[{len(chars)} characters]" if chars else "")
        output.write(f"\r\x1b[K{PASTE_PROMPT}{shown}")
        output.flush()

    with raw_mode():
        draw()
        while True:
            key = keys()
            if key in ("escape", "ctrl-c"):
                output.write("\r\n")
                return None
            if key == "enter":
                if parse_pasteback("".join(chars)) is not None:
                    output.write("\r\n")
                    return "".join(chars)
                note = "no authorization code in that; paste the whole address"
            elif key == "backspace":
                chars[-1:] = []
                note = ""
            elif key == "ctrl-u":
                chars, note = [], ""
            elif len(key) == 1 and key.isprintable():
                chars.append(key)
                note = ""
            else:
                continue
            draw()


def sign_out(source: ClaudeCodeConfig, profile: str | None = None) -> str:
    """Forget the stored tokens; runs with `claude-code:` models then ask to sign in again."""
    source.store(profile).delete()
    return f"Signed out of {_account(profile)}. The tokens stay valid at Anthropic until they expire."


async def configure(source: ClaudeCodeConfig, show_url: Callable[[str], None], runners: Runners = TERMINAL) -> str:
    """Open the settings menu until Esc or Save & close."""

    async def account() -> list[str]:
        try:
            return [await sign_in(source, show_url)]
        except UserError as exc:
            return [str(exc)]

    messages = await run_flow_async(FieldMenu(source), runners, submenus={"account": account})
    return "\n".join(messages) or "Claude Code settings unchanged."


class ClaudeCodePlugin(Plugin[ClaudeCodeSettings]):
    """`claude-code:` models, `/login claude-code`, the settings menu, `/claude_code`, and the update notice."""

    def __init__(self, host: PluginHost, settings: ClaudeCodeSettings) -> None:
        """Build the provider and the menu's source once; CLAI2 loads the plugin again when settings change."""
        super().__init__(host, settings)
        self.source = ClaudeCodeConfig(settings, host.save_settings)
        self._providers = Providers()
        self._update: asyncio.Task[str | None] | None = None
        self.provider = ModelProvider(
            prefix=PREFIX,
            resolve=self._resolve,
            models=config.MODELS,
            settings_from="anthropic",
            **_supported(ModelProvider, resolve_profile=self._resolve_profile),
        )

    def _resolve(self, name: str) -> ClaudeCodeModel:
        return self._providers.model(name, self.source)

    def _resolve_profile(self, name: str, profile: str) -> ClaudeCodeModel:
        return self._providers.model(name, self.source, profile)

    def _show_url(self, url: str) -> None:
        self.host.console.print(
            "Opening your browser to sign in to Claude Code. If it does not open, or this machine's browser "
            f"cannot reach it (SSH, a remote box), open this anywhere and sign in:\n{url}",
            markup=False,
            soft_wrap=True,
        )

    def get_model_providers(self) -> Sequence[ModelProvider]:
        return (self.provider,)

    def get_logins(self) -> Sequence[PluginLogin]:
        return (
            PluginLogin(
                name=LOGIN,
                handler=self._sign_in,
                models=self.provider.names,
                **_supported(PluginLogin, profile_handler=self._sign_in_profile, usage=self._usage),
            ),
        )

    async def _usage(self, profile: str | None) -> Any:
        provider = await asyncio.to_thread(self._providers.provider, self.source, profile)
        return await account_usage(provider)

    async def _sign_in(self) -> str:
        return await sign_in(self.source, self._show_url)

    async def _sign_in_profile(self, profile: str) -> str:
        return await sign_in(self.source, self._show_url, profile)

    def get_commands(self) -> Sequence[Command]:
        return (
            Command(
                name=COMMAND,
                description=f"Claude Code settings: sign-in, credential storage, logout, and status. "
                f"Sign in with /login {LOGIN}.",
                handler=self._command,
                complete=lambda args: (
                    [word for word in ("logout", "status") if word.startswith(args[0] if args else "")]
                    if len(args) <= 1
                    else []
                ),
            ),
        )

    async def _command(self, args: list[str]) -> str:
        if not args:
            return await self.configure()
        action = {"logout": sign_out, "status": status}.get(args[0])
        if action is None or len(args) > 2:
            raise ValueError(_HELP)
        return await asyncio.to_thread(action, self.source, args[1] if len(args) == 2 else None)

    async def configure(self) -> str:
        return await configure(self.source, self._show_url)

    async def on_session_start(self, event: SessionStart) -> None:
        """Check PyPI for a newer release in the background, so startup never waits on it."""
        if updates.enabled():
            self._update = asyncio.create_task(updates.newer_release(__version__))

    async def on_turn_end(self, event: TurnEnd) -> None:
        """Mention a newer release once, after the first turn that finishes once the check is done."""
        if self._update is None or not self._update.done():
            return
        latest, self._update = self._update.result(), None
        if latest is not None:
            self.host.console.print(updates.notice(__version__, latest), markup=False, soft_wrap=True)

    async def on_session_end(self, event: SessionEnd) -> None:
        if self._update is not None:
            self._update.cancel()
