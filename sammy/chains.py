# Grown from clai2 4698fe899 (pydantic_clai2/models/chains.py): the operator names the chains in settings
# (`MODEL_CHAINS`) instead of the `/model` picker, and each fallback is logged as a warning.
"""Fallback chains: `MODEL=chain:NAME` runs the chain's models in order, moving on when one fails.

Pydantic AI's `FallbackModel` does the work; a chain only names the models. A request moves to the next model on a
provider error, such as an outage or a rate or usage limit (`ModelAPIError`), or on an expired Claude Code sign-in;
any other error fails the run as before. The model span shows which model answered, and each move is a warning.

A model request is one DBOS step whichever model answers it, so a replay returns the recorded response and calls no
model at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import logfire
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from sammy.vendor.claude_code import ClaudeCodeSignInExpiredError

PREFIX = 'chain'
FALLBACK_ON = (ModelAPIError, ClaudeCodeSignInExpiredError)


def chain_name(model: str) -> str | None:
    """`NAME` for `chain:NAME`, else `None`."""
    prefix, separator, name = model.partition(':')
    return name if separator and prefix == PREFIX else None


def check_member(model: str) -> str:
    """A model that is not itself a chain; raises `ValueError` otherwise."""
    if chain_name(model) is not None:
        raise ValueError(f'{model}: a chain cannot contain another chain.')
    return model


def fall_back(error: Exception) -> bool:
    """Whether the next model takes over from one that failed with `error`, with a warning when it does. The error's
    type and model only: its text can quote the user's content."""
    if not isinstance(error, FALLBACK_ON):
        return False
    logfire.warn(
        '{model} failed with {error_type}; the next model in the chain takes over',
        model=error.model_name if isinstance(error, ModelAPIError) else 'claude-code',
        error_type=type(error).__qualname__,
    )
    return True


def chain(models: Sequence[Model | str]) -> Model | str:
    """The models in order, each taking over when the one before fails. A chain of one is that model."""
    first, *rest = models
    return FallbackModel(first, *rest, fallback_on=fall_back) if rest else first
