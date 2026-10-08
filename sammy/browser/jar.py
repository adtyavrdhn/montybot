"""Where the browser service keeps each user's saved sign-ins, and the lease that allows one writer at a time.

Both are interfaces with in-memory stand-ins. The real ones, in Postgres with the state encrypted per user, belong to
#4; they replace `InMemoryJar` and `InMemoryJarLease` without changes to the browser service.

The lease is per user, held per run. Only runs that hold it may start a browser from the user's jar or save to it, so
two browsers never save over each other. Runs share it when they share one browser: each in its own tab of the user's
browser, on one server (`shared=True`, the lease's `owner`). Otherwise one run holds it at a time. A hand-off happens
inside its run's browser, so it shares the run's lease.
"""

from __future__ import annotations

from typing import Protocol

from sammy.browser.service import RunId, UserId
from sammy.browser.state import BrowserState


class SignInJar(Protocol):
    """Each user's saved `BrowserState`. A saved state is a set of live credentials: never log it."""

    async def load(self, *, user_id: UserId) -> BrowserState | None:
        """The user's latest saved state, or None if nothing was ever saved."""
        ...

    async def save(self, *, user_id: UserId, state: BrowserState) -> None:
        """Replace the user's saved state. The caller holds the user's lease."""
        ...


class JarLease(Protocol):
    """Which runs may use the user's jar: one at a time, or several sharing one browser on this lease's owner."""

    async def acquire(self, *, user_id: UserId, run_id: RunId, shared: bool = False) -> bool:
        """Take the user's lease for `run_id`. True if `run_id` holds it now, including when it already did. With
        `shared`, other runs holding it shared on the same owner are no obstacle; any other holder is."""
        ...

    async def holds(self, *, user_id: UserId, run_id: RunId) -> bool:
        """Whether `run_id` holds the user's lease. The service uses it to find its runs again after a restart."""
        ...

    async def release(self, *, user_id: UserId, run_id: RunId) -> None:
        """Give the lease back. Does nothing if `run_id` does not hold it."""
        ...


class InMemoryJar:
    """A `SignInJar` in a dict, standing in for #4's encrypted Postgres table. Stores copies, so callers cannot
    change a saved state by accident."""

    def __init__(self) -> None:
        self._states: dict[UserId, BrowserState] = {}
        self.saves = 0
        """How many times `save` was called, for tests."""

    async def load(self, *, user_id: UserId) -> BrowserState | None:
        state = self._states.get(user_id)
        return None if state is None else BrowserState.from_json(state.to_json())

    async def save(self, *, user_id: UserId, state: BrowserState) -> None:
        self._states[user_id] = BrowserState.from_json(state.to_json())
        self.saves += 1


class InMemoryJarLease:
    """A `JarLease` in a dict, standing in for #4's lease. It lives as long as the process, and has one owner."""

    def __init__(self) -> None:
        self._holders: dict[UserId, dict[RunId, bool]] = {}
        """Each user's holding runs, and whether each holds it shared."""

    async def acquire(self, *, user_id: UserId, run_id: RunId, shared: bool = False) -> bool:
        holders = self._holders.setdefault(user_id, {})
        others = [other_shared for other, other_shared in holders.items() if other != run_id]
        if others and not (shared and all(others)):
            return run_id in holders
        holders[run_id] = shared
        return True

    async def holds(self, *, user_id: UserId, run_id: RunId) -> bool:
        return run_id in self._holders.get(user_id, {})

    async def release(self, *, user_id: UserId, run_id: RunId) -> None:
        self._holders.get(user_id, {}).pop(run_id, None)
