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
        """For a connect ask, what it offers to connect: `provider`, `key`, `name`, `logo`, and `server_id` for an MCP
        server to sign in to again (`montybot.integration_tools.offer_of`)."""
        offered: dict[str, str] = self.details.get('integration') or {}
        return offered
