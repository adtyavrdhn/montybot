# Grown from viktor c1896df (viktor/deps.py): a run belongs to one user, and its tools are bound to it.
from __future__ import annotations

from dataclasses import dataclass, field

from sammy.models import Run, Schedule
from sammy.resources import Resources


@dataclass
class Asked:
    """How many times this run has asked the user something. Counted in workflow code, which DBOS replays in the
    same order after a restart, so the n-th ask of a run always has the same id and topic."""

    count: int = 0

    def next(self) -> int:
        self.count += 1
        return self.count


@dataclass
class Steered:
    """Whether this run reads the messages the user sends while it works (`sammy.steering`), and how many model
    requests have looked for them. Off for a run recorded before steering: its replay must make the same steps.
    Counted in workflow code, which DBOS replays in the same order."""

    on: bool = False
    count: int = 0

    def next(self) -> int:
        self.count += 1
        return self.count


@dataclass
class CodeState:
    """The run's Monty session between `run_code` calls: an id in monty-server's store (or bytes, locally). Set from
    each `run_code` step's recorded result, so a replay restores it."""

    state: bytes | None = None


@dataclass
class Notified:
    """Whether this run's watch has told the user what it found. Set in workflow code, which DBOS replays in order."""

    done: bool = False


@dataclass
class Connected:
    """The user's integrations, in words, as this run last looked them up (`sammy.integration_tools`); None to look
    again. Set in workflow code from a step's recorded result, so a replay sees the same."""

    text: str | None = None


@dataclass(frozen=True)
class RunDeps:
    resources: Resources
    run: Run
    schedule: Schedule | None = None
    """The schedule whose occurrence this run is, if a schedule started it."""
    local_time: str = ''
    """The user's date and time when the run started, in words, with their time zone."""
    squirrel_name: str = ''
    """What the user named their squirrel (Sammy's mascot in the Mac app), when the run started; empty if unnamed."""
    asked: Asked = field(default_factory=Asked)
    code: CodeState = field(default_factory=CodeState)
    notified: Notified = field(default_factory=Notified)
    connected: Connected = field(default_factory=Connected)
    steered: Steered = field(default_factory=Steered)

    @property
    def user_id(self) -> str:
        return self.run.user_id

    @property
    def run_id(self) -> str:
        return self.run.id
