# Grown from viktor c1896df (viktor/models.py): users own everything, so workspaces and Slack ids are gone.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RunStatus = Literal['queued', 'running', 'waiting', 'done', 'failed']
Trigger = Literal['message', 'schedule']
AskKind = Literal['question', 'approval', 'handoff']
NoticeKind = AskKind | Literal['finished', 'found']
"""What a notification tells the user: an ask, a scheduled task that finished, or a watch that found something."""
ACTIVE: tuple[RunStatus, ...] = ('queued', 'running', 'waiting')


@dataclass(frozen=True, kw_only=True)
class User:
    id: str
    email: str
    name: str


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
