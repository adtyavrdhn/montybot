"""Where the browser service keeps each user's saved sign-ins, and the lease that allows one writer at a time.

Both are interfaces with in-memory stand-ins. The real ones, in Postgres with the state encrypted per user, belong to
#4; they replace `InMemoryJar` and `InMemoryJarLease` without changes to the browser service.

The lease is per user. The run that holds it is the only one that may start a browser from the user's jar or save to
it, so two runs never save over each other. A hand-off happens inside its run's browser, so it shares the run's lease.
"""

from __future__ import annotations

from typing import Protocol

from montybot.browser.service import RunId, UserId
from montybot.browser.state import BrowserState


class SignInJar(Protocol):
    """Each user's saved `BrowserState`. A saved state is a set of live credentials: never log it."""

    async def load(self, *, user_id: UserId) -> BrowserState | None:
        """The user's latest saved state, or None if nothing was ever saved."""
        ...

    async def save(self, *, user_id: UserId, state: BrowserState) -> None:
        """Replace the user's saved state. The caller holds the user's lease."""
        ...


class JarLease(Protocol):
    """One run at a time per user may use the user's jar."""

    async def acquire(self, *, user_id: UserId, run_id: RunId) -> bool:
        """Take the user's lease for `run_id`. True if `run_id` holds it now, including when it already did; False
        if another run holds it."""
        ...

    async def holder(self, *, user_id: UserId) -> RunId | None:
        """The run holding the user's lease, if any. The service uses it to find its runs again after a restart."""
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
    """A `JarLease` in a dict, standing in for #4's lease. It lives as long as the process; #4's version also needs
    an expiry, so a lease held by a process that died is freed."""

    def __init__(self) -> None:
        self._holders: dict[UserId, RunId] = {}

    async def acquire(self, *, user_id: UserId, run_id: RunId) -> bool:
        holder = self._holders.setdefault(user_id, run_id)
        return holder == run_id

    async def holder(self, *, user_id: UserId) -> RunId | None:
        return self._holders.get(user_id)

    async def release(self, *, user_id: UserId, run_id: RunId) -> None:
        if self._holders.get(user_id) == run_id:
            del self._holders[user_id]
