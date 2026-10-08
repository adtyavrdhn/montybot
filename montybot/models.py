# Grown from viktor c1896df (viktor/models.py): users own everything, so workspaces and Slack ids are gone.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

RunStatus = Literal['queued', 'running', 'waiting', 'done', 'failed', 'stopped']
Trigger = Literal['message', 'schedule']
AskKind = Literal['question', 'approval', 'handoff', 'connect']
NoticeKind = AskKind | Literal['finished', 'failed', 'found']
"""What a notification tells the user: an ask, a scheduled task that ended, or a watch that found something."""
ACTIVE: tuple[RunStatus, ...] = ('queued', 'running', 'waiting')
FINISHED: tuple[RunStatus, ...] = ('done', 'failed', 'stopped')


@dataclass(frozen=True, kw_only=True)
class User:
    id: str
    email: str
    name: str
    timezone: str = 'UTC'
    """An IANA zone, as the user's browser last reported it."""
    squirrel_name: str = ''
    """What the user named their squirrel in the Mac app, which is what Monty answers to. Empty until they pick one."""


@dataclass(frozen=True, kw_only=True)
class Thread:
    id: str
    user_id: str
    title: str


@dataclass(frozen=True, kw_only=True)
class Run:
    id: str
    user_id: str
    thread_id: str
    trigger: Trigger
    prompt: str
    status: RunStatus
    output: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


AttachmentKind = Literal['image', 'pdf', 'text', 'file']
"""How the model reads a file: it sees an image or a PDF, reads a text file, and opens any other with its code."""


@dataclass(frozen=True, kw_only=True)
class Attachment:
    """A file in a chat: one the user attached to their message, or one Monty shared with its reply."""

    id: str
    name: str
    media_type: str
    kind: AttachmentKind
    size: int
    path: str | None = None
    """Where the run's code sees the user's file, once the run has started."""

    def json(self) -> dict[str, str | int]:
        return {'id': self.id, 'name': self.name, 'media_type': self.media_type, 'kind': self.kind, 'size': self.size}


@dataclass(frozen=True, kw_only=True)
class Schedule:
    id: str
    user_id: str
    thread_id: str
    name: str
    cron: str
    timezone: str
    when: str
    """The schedule in plain words, as the agent put it to the user: "Mondays at 09:00"."""
    prompt: str
    watch: bool


@dataclass(frozen=True, kw_only=True)
class Ask:
    id: str
    run_id: str
    user_id: str
    occurrence: int
    kind: AskKind
    prompt: str
    details: dict[str, Any]
    answer: dict[str, Any] | None

    @property
    def integration(self) -> dict[str, str]:
        """For a connect ask, what it offers to connect: `provider`, `key`, `name`, `logo`; for a listed MCP server,
        `url` and `auth` (and `token_hint` and `token_header` for a token); `server_id` for an MCP server to sign in to
        again (`montybot.integration_tools.offer_of`)."""
        offered: dict[str, str] = self.details.get('integration') or {}
        return offered
