"""What Composio apps and MCP servers have in common, for the agent: tools, and errors it can read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class IntegrationError(Exception):
    """An integration refused or failed. The message is for the user and the model: it never holds a secret."""


@dataclass(frozen=True, kw_only=True)
class Tool:
    name: str
    """What the agent calls: `LINEAR_CREATE_LINEAR_ISSUE`, or an MCP server's own tool name."""
    title: str
    description: str
    parameters: dict[str, Any]
    """JSON schema of the arguments."""
    read_only: bool
    """It only reads (Composio's `readOnlyHint` tag, MCP's `readOnlyHint` annotation): no approval needed."""
    version: str = ''
    """Composio's version of the tool, run as listed; empty for MCP."""
