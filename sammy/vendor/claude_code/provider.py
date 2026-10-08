"""Pydantic AI provider backed by a Claude Code subscription."""

from __future__ import annotations

import httpx2

from anthropic import AsyncAnthropic

from pydantic_ai.exceptions import UserError
from pydantic_ai.providers.anthropic import AnthropicProvider

from . import config
from .auth import CredentialsRefreshCallback, _ClaudeCodeAuth
from .credentials import ClaudeCodeCredentials
from .storage import TokenStore, default_store, refresh_lock_path


class ClaudeCodeProvider(AnthropicProvider):
    """Anthropic provider that authenticates with Claude Code subscription tokens.

    Mirrors the `OpenAICodexProvider` pattern from pydantic-ai: the provider owns
    an httpx2 auth shim that injects the bearer token, refreshes it before expiry,
    and retries once on 401.
    """

    @property
    def name(self) -> str:
        return "claude-code"

    def __init__(
        self,
        credentials: ClaudeCodeCredentials | None = None,
        *,
        store: TokenStore | None = None,
        on_credentials_refresh: CredentialsRefreshCallback | None = None,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        """Create a Claude Code provider.

        Args:
            credentials: Credentials to use. When omitted, they are loaded from
                the store (which defaults to the OS keyring, falling back to a file).
            store: Token store used to persist refreshed credentials. When
                credentials are loaded from the store, refreshes are persisted
                back to it automatically. With a store, a refresh first adopts
                tokens another process saved, under a lock all processes share,
                since each refresh revokes the previous refresh token.
            on_credentials_refresh: Optional callback in place of automatic
                persistence. Takes precedence over `store` when both are given.
            http_client: An existing httpx2 client to use; its `auth` is replaced.
            base_url: Override for the Anthropic API base URL.
        """
        if credentials is None:
            if store is None:
                store = default_store()
            creds = store.load()
            if creds is None:
                raise UserError(
                    "No Claude Code credentials found. Run `asyncio.run(pydantic_ai_claude_code.login())` "
                    "to authenticate, or pass credentials to the provider."
                )
            credentials = creds
        if on_credentials_refresh is None and store is not None:

            async def persist(updated: ClaudeCodeCredentials) -> None:
                store.save(updated)

            on_credentials_refresh = persist
        auth = _ClaudeCodeAuth(
            credentials,
            on_credentials_refresh,
            reload=None if store is None else store.load,
            lock_path=None if store is None else refresh_lock_path(),
        )
        if http_client is None:
            http_client = httpx2.AsyncClient(auth=auth)
        else:
            http_client.auth = auth  # type: ignore[assignment]
        self._claude_code_auth = auth
        self._client = AsyncAnthropic(
            api_key="unused",  # the auth shim supplies the real credentials
            base_url=base_url or config.API_BASE_URL,
            http_client=http_client,
        )

    @property
    def credentials(self) -> ClaudeCodeCredentials:
        """The current credentials; refreshed in place by the auth shim."""
        return self._claude_code_auth.credentials

    @classmethod
    def from_token_store(
        cls,
        *,
        store: TokenStore | None = None,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> "ClaudeCodeProvider":
        """Build a provider from the default store."""
        return cls(store=store, http_client=http_client, base_url=base_url)
