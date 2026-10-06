"""Finding a hand-off by its id, and reporting that the user gave the browser back.

The data model (#4) stores hand-offs and the run (#3, #5) waits for the give-back; until they exist,
`InMemoryHandoffs` stands in for both.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from montybot.browser.service import Handoff, HandoffEnded, HandoffId


@dataclass(frozen=True, kw_only=True)
class GiveBack:
    """What the run gets when the user gives the browser back: words only, never a screenshot."""

    handoff: Handoff
    ended: HandoffEnded
    summary: str
    """What the user did, for the agent: counts of clicks, keys and so on, and where they left the browser. Never
    what they typed."""


class Handoffs(Protocol):
    async def find(self, handoff_id: HandoffId) -> Handoff | None:
        """The hand-off with this id, active or not, or None."""
        ...

    async def given_back(self, given: GiveBack) -> None:
        """The user gave the browser back and the browser service has saved the state: wake the run."""
        ...


class InMemoryHandoffs:
    """Stands in for #4's hand-off table and the run's wait (`DBOS.recv` in DESIGN.md)."""

    def __init__(self) -> None:
        self._handoffs: dict[HandoffId, Handoff] = {}
        self._given: dict[HandoffId, asyncio.Future[GiveBack]] = {}

    def add(self, handoff: Handoff) -> None:
        self._handoffs[handoff.handoff_id] = handoff

    async def find(self, handoff_id: HandoffId) -> Handoff | None:
        return self._handoffs.get(handoff_id)

    async def given_back(self, given: GiveBack) -> None:
        future = self._future(given.handoff.handoff_id)
        if not future.done():
            future.set_result(given)

    async def wait_given_back(self, handoff_id: HandoffId) -> GiveBack:
        return await asyncio.shield(self._future(handoff_id))

    def _future(self, handoff_id: HandoffId) -> asyncio.Future[GiveBack]:
        if handoff_id not in self._given:
            self._given[handoff_id] = asyncio.get_running_loop().create_future()
        return self._given[handoff_id]
