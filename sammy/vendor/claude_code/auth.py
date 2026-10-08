"""httpx2 auth shim that authenticates Messages API calls with Claude Code tokens."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2

from pydantic_ai.exceptions import UserError

from . import config
from .credentials import ClaudeCodeCredentials
from .flow import refresh_credentials

CredentialsRefreshCallback = Callable[[ClaudeCodeCredentials], Awaitable[None]]
CredentialsReload = Callable[[], ClaudeCodeCredentials | None]

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no cross-process lock, the store re-read still helps
    fcntl = None


class ClaudeCodeCredentialsPersistenceError(UserError):
    """Raised after refreshed in-memory credentials could not be persisted."""


class ClaudeCodeSignInExpiredError(UserError):
    """The stored sign-in can no longer be refreshed, so the user has to sign in again.

    Raised inside the HTTP client, where the Anthropic SDK reports it as a connection error;
    `ClaudeCodeModel` unwraps it again so callers see this message instead.
    """

    def __init__(self, reason: str) -> None:
        """`reason` is why the refresh failed, as the token endpoint put it."""
        super().__init__(
            f"Your Claude Code sign-in has expired or was revoked; sign in again. ({reason}) "
            "In CLAI2 run /login claude-code; from Python, `await pydantic_ai_claude_code.login()` "
            "or `python -m pydantic_ai_claude_code login`."
        )


def _expires_soon(credentials: ClaudeCodeCredentials) -> bool:
    if credentials.expires_at is None:
        return False
    return time.time() + 30 >= credentials.expires_at.timestamp()


@asynccontextmanager
async def _exclusive(path: Path | None) -> AsyncIterator[None]:
    """Hold an exclusive `flock` on `path` across processes; a no-op without a path or `fcntl`."""
    if path is None or fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


class _ClaudeCodeAuth(httpx2.Auth):
    """Bearer auth that refreshes the token pair, coordinating with every other holder of the same sign-in.

    Anthropic rotates the refresh token on each refresh and revokes the old one, so a process still holding
    the old one gets `invalid_grant`. With `reload` (the token store's `load`), a refresh first adopts a
    pair another process already saved, and `lock_path` serializes the reload, refresh, and save across
    processes, so only one of them spends a given refresh token.
    """

    requires_response_body = True

    def __init__(
        self,
        credentials: ClaudeCodeCredentials,
        callback: CredentialsRefreshCallback | None = None,
        *,
        reload: CredentialsReload | None = None,
        lock_path: Path | None = None,
    ) -> None:
        self.credentials = credentials
        self.callback = callback
        self.reload = reload
        self.lock_path = lock_path
        self.revision = 0
        self.lock = asyncio.Lock()
        self.refresh_client = httpx2.AsyncClient()

    async def _adopt_stored(self) -> bool:
        """Switch to the stored pair when another holder saved a newer one; whether it did."""
        if self.reload is None:
            return False
        stored = await asyncio.to_thread(self.reload)
        if stored is None or stored == self.credentials:
            return False
        self.credentials = stored
        self.revision += 1
        return True

    async def _refresh(self, used_revision: int) -> None:
        async with self.lock, _exclusive(self.lock_path):
            if self.revision != used_revision:
                return
            if await self._adopt_stored() and not _expires_soon(self.credentials):
                return
            try:
                updated = await refresh_credentials(self.credentials, http_client=self.refresh_client)
            except UserError as exc:
                if await self._adopt_stored():
                    return  # rotated by a holder outside the lock: another machine, or Windows
                raise ClaudeCodeSignInExpiredError(str(exc)) from exc
            self.credentials = updated
            self.revision += 1
            if self.callback is not None:
                try:
                    await self.callback(updated)
                except Exception as exc:  # noqa: BLE001 - surface the persistence failure to the caller
                    raise ClaudeCodeCredentialsPersistenceError(
                        "Claude Code credentials refreshed in memory, but the persistence callback failed."
                    ) from exc

    def _apply(self, request: httpx2.Request) -> int:
        # Subscription tokens are `Authorization: Bearer` credentials, not API keys.
        # The SDK injects `x-api-key` from the placeholder `api_key`, so it must be
        # removed or the server validates it first and rejects it as an API key.
        if "x-api-key" in request.headers:
            del request.headers["x-api-key"]
        request.headers["Authorization"] = f"Bearer {self.credentials.token}"
        request.headers["x-app"] = config.X_APP
        request.headers["user-agent"] = config.USER_AGENT
        # Anthropic's SDK manages its own betas; ours must be merged, not replaced.
        existing_beta = request.headers.get("anthropic-beta")
        if config.ANTHROPIC_BETA not in (existing_beta or ""):
            request.headers["anthropic-beta"] = ", ".join(filter(None, [existing_beta, config.ANTHROPIC_BETA]))

        return self.revision

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        revision = self._apply(request)
        if _expires_soon(self.credentials):
            await self._refresh(revision)
            revision = self._apply(request)
        response = yield request
        if response.status_code != 401:
            return
        await response.aread()
        await response.aclose()
        await self._refresh(revision)
        self._apply(request)
        yield request
