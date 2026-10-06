"""Credentials for Claude Code subscription authentication."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from pydantic import BaseModel, SecretStr

from pydantic_ai.exceptions import UserError


class ClaudeCodeCredentials(BaseModel):
    """Credentials minted by the Claude Code OAuth flow.

    `access_token` is the bearer token sent to the Messages API. `refresh_token`
    is used by the auth layer to mint a new pair before expiry.
    """

    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: datetime | None = None

    @property
    def token(self) -> str:
        """The plaintext access token."""
        return self.access_token.get_secret_value()

    def to_wire_dict(self) -> dict[str, object]:
        """A JSON-serializable, plain-string form of the credentials for storage.

        Pydantic 2.13 masks `SecretStr` in some serialization modes, so the stored
        form is spelled out here instead of trusting `model_dump`.
        """
        return {
            "access_token": self.token,
            "refresh_token": self.refresh_token.get_secret_value(),
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
        }

    @classmethod
    def from_token_response(cls, data: object, previous: ClaudeCodeCredentials | None = None) -> ClaudeCodeCredentials:
        """Build credentials from a token-endpoint response.

        Args:
            data: The parsed JSON body of a token response.
            previous: Prior credentials whose refresh token is reused when the
                issuer omits a new one, as Anthropic's refresh responses do.
        """
        root: dict[object, object] = data if isinstance(data, dict) else {}
        access_token = root.get("access_token")
        refresh_token = root.get("refresh_token")
        if not isinstance(access_token, str):
            raise UserError("Claude Code token response did not contain a string `access_token`.")
        if not isinstance(refresh_token, str):
            if previous is None:
                raise UserError("Claude Code token response did not contain a string `refresh_token`.")
            refresh_token = previous.refresh_token.get_secret_value()
        expires_at = None
        if expires_in := root.get("expires_in"):
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                expires_at = datetime.fromtimestamp(time.time() + float(expires_in), tz=timezone.utc)
        return cls(
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token),
            expires_at=expires_at,
        )

    @classmethod
    def from_token_file(cls, data: object) -> ClaudeCodeCredentials:
        """Parse a stored token file (the saved output of a previous exchange)."""
        return cls.model_validate(data)
