# Grown from viktor c1896df (viktor/deps.py): a run belongs to one user, and its tools are bound to it.
from __future__ import annotations

from dataclasses import dataclass, field

from montybot.models import Run
from montybot.resources import Resources


@dataclass
class Asked:
    """How many times this run has asked the user something. Counted in workflow code, which DBOS replays in the
    same order after a restart, so the n-th ask of a run always has the same id and topic."""

    count: int = 0

    def next(self) -> int:
        self.count += 1
        return self.count


@dataclass(frozen=True)
class RunDeps:
    resources: Resources
    run: Run
    asked: Asked = field(default_factory=Asked)

    @property
    def user_id(self) -> str:
        return self.run.user_id

    @property
    def run_id(self) -> str:
        return self.run.id
