"""Tell CLAI2 users when PyPI has a newer release of the plugin than the copy they run.

Set `CLAUDE_CODE_NO_UPDATE_CHECK=1` to skip the check. Network problems are silent: an update notice is
not worth an error.
"""

from __future__ import annotations

import os
import re

import httpx2
from pydantic import BaseModel, ValidationError

RELEASES_URL = "https://pypi.org/pypi/pydantic-claude-code/json"
INSTALL = (
    "curl -fsSL https://gist.githubusercontent.com/mpfaffenberger/bd75c7b424ffdc6d81645bfe7de822d3/raw/install.sh | sh"
)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class _Info(BaseModel):
    version: str


class _Release(BaseModel):
    info: _Info


def _parse(version: str) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(version)
    return tuple(int(part) for part in match.groups()) if match else None


def enabled() -> bool:
    """Whether to check at all; `CLAUDE_CODE_NO_UPDATE_CHECK` set to anything but `0` turns it off."""
    return os.environ.get("CLAUDE_CODE_NO_UPDATE_CHECK", "0") == "0"


async def newer_release(current: str, *, timeout: float = 5) -> str | None:
    """The latest release's version when it is newer than `current`; `None` when not, or unknown."""
    try:
        async with httpx2.AsyncClient(timeout=timeout) as client:
            response = await client.get(RELEASES_URL)
            response.raise_for_status()
        latest = _Release.model_validate_json(response.content).info.version
    except (httpx2.HTTPError, ValidationError):
        return None
    latest_parts, current_parts = _parse(latest), _parse(current)
    if latest_parts is None or current_parts is None or latest_parts <= current_parts:
        return None
    return latest


def notice(current: str, latest: str) -> str:
    """The one line shown when an update is out."""
    return f"Claude Code plugin {latest} is out (you have {current}). Update: {INSTALL}"
