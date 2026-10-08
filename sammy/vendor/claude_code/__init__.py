"""Use your Claude Code subscription from a Pydantic AI Agent, or as a CLAI2 plugin.

This folder is also a complete CLAI2 drop-in plugin: copy it into CLAI2's plugins folder as `claude_code/`
and CLAI2 loads `ClaudeCodePlugin` below. It needs nothing CLAI2 does not already install.

Quick start:

    from pydantic_ai import Agent
    from pydantic_ai_claude_code import ClaudeCodeModel

    agent = Agent(ClaudeCodeModel('claude-fable-5-1'))
"""

from __future__ import annotations

from .auth import ClaudeCodeSignInExpiredError
from .credentials import ClaudeCodeCredentials
from .flow import (
    ClaudeCodeOAuthFlow,
    exchange_code,
    login,
    parse_pasteback,
    refresh_credentials,
)
from .model import ClaudeCodeModel
from .provider import ClaudeCodeProvider
from .storage import ClaudeCodeTokenStore, KeyringTokenStore, default_auth_path, default_store

__version__ = "0.7.1"
"""Matches `version` in pyproject.toml (a test checks), so a copied folder knows its release."""


try:
    from .clai2 import ClaudeCodePlugin as _ClaudeCodePlugin
except ModuleNotFoundError as error:  # plain Pydantic AI use: CLAI2 is not installed, and needs no plugin
    if not (error.name or "").startswith("pydantic_clai2"):
        raise
else:

    class ClaudeCodePlugin(_ClaudeCodePlugin):
        """The CLAI2 plugin, declared here because CLAI2 loads the `Plugin` a drop-in's `__init__.py` defines."""


__all__ = [
    "ClaudeCodeCredentials",
    "ClaudeCodeModel",
    "ClaudeCodeOAuthFlow",
    "ClaudeCodeProvider",
    "ClaudeCodeSignInExpiredError",
    "ClaudeCodeTokenStore",
    "KeyringTokenStore",
    "__version__",
    "default_auth_path",
    "default_store",
    "exchange_code",
    "login",
    "parse_pasteback",
    "refresh_credentials",
]
