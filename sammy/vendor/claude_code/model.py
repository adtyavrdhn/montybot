"""The Claude Code model: an `AnthropicModel` with the Claude Code persona prepended."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, SystemPromptPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers import Provider
from pydantic_ai.settings import ModelSettings

from . import config
from .auth import ClaudeCodeSignInExpiredError


class ClaudeCodeModel(AnthropicModel):
    """AnthropicModel whose system prompt opens with the Claude Code persona.

    The subscription backend expects the Claude Code persona at position 0 of
    the system context. Prepending a `SystemPromptPart` here is idempotent:
    once it's in history it stays, and repeated `prepare_messages` calls don't
    duplicate it.
    """

    def __init__(
        self,
        model_name: str,
        *,
        provider: Provider[Any] | None = None,
        settings: ModelSettings | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a Claude Code model.

        Args:
            model_name: The model name to use, e.g. `'claude-fable-5-1'`.
            provider: The provider to authenticate with. Defaults to a
                `ClaudeCodeProvider` backed by the default token store.
        """
        if provider is None:
            from .provider import ClaudeCodeProvider

            provider = ClaudeCodeProvider()
        defaults = AnthropicModelSettings(
            anthropic_cache=False if settings and settings.get("anthropic_cache_messages") else "1h",
        )
        super().__init__(model_name, provider=provider, settings={**defaults, **(settings or {})}, **kwargs)

    def prepare_messages(
        self,
        messages: list[ModelMessage],
        model_request_parameters=None,
    ) -> list[ModelMessage]:
        return super().prepare_messages(_prepend_persona(messages), model_request_parameters)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        with _surface_expired_sign_in():
            return await super().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        with _surface_expired_sign_in():
            async with super().request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response


@contextmanager
def _surface_expired_sign_in() -> Iterator[None]:
    """Re-raise an expired sign-in that the SDK reported as `Connection error.`."""
    try:
        yield
    except ModelAPIError as exc:
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, ClaudeCodeSignInExpiredError):
                raise cause from exc
            cause = cause.__cause__
        raise


def _prepend_persona(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Return `messages` with the persona added first, unless it's already there."""
    for index, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        if any(
            isinstance(part, SystemPromptPart) and part.content.strip().startswith(config.CLAUDE_CODE_SYSTEM_PROMPT)
            for part in message.parts
        ):
            return messages
        updated = ModelRequest([SystemPromptPart(config.CLAUDE_CODE_SYSTEM_PROMPT), *message.parts])
        return [*messages[:index], updated, *messages[index + 1 :]]
    return messages
