"""The browser service API: how the agent and the web app reach a run's browser.

Only the interface; #10 implements it. The service is one long-lived process that owns every browser, so a browser
outlives the agent attempt that started it and survives a pause for a hand-off. It drives each browser through a
`BrowserBackend` and saves `BrowserState` to the user's sign-in jar (#4).

Every call names the run and the user. The service checks that the run belongs to the user and answers `UnknownRun`
otherwise, so a caller cannot tell another user's run from one that does not exist. Monty never sees these ids: the
host functions it calls are bound to one run and fill them in.

How the wire looks (HTTP, a socket, in-process) is #10's choice. Every argument and result is a dataclass of plain
values, and actions carry `kind`, so they encode to JSON without guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from montybot.browser.contract import Action, BrowserError, Screenshot, Snapshot

RunId = str
UserId = str
HandoffId = str


@dataclass(frozen=True, kw_only=True)
class Restarted:
    """The run's browser was gone (idle reaper, crash, server restart) and was started again from the user's saved
    state. Pass this on to the agent: the page was reloaded, and anything not saved, such as a half-filled form, is
    lost."""

    reason: str
    url: str
    """Where the new browser is now: the URL in the saved state."""


@dataclass(frozen=True, kw_only=True)
class Started:
    url: str
    """The page the browser is on."""
    reused: bool
    """True when the run already had a browser, as on a retry or after a pause."""
    restarted: Restarted | None = None


@dataclass(frozen=True, kw_only=True)
class ActionResult:
    restarted: Restarted | None = None
    """Set when the browser had to be restarted before this action ran."""


@dataclass(frozen=True, kw_only=True)
class SnapshotResult:
    snapshot: Snapshot
    restarted: Restarted | None = None


@dataclass(frozen=True, kw_only=True)
class ScreenshotResult:
    screenshot: Screenshot
    restarted: Restarted | None = None


@dataclass(frozen=True, kw_only=True)
class Handoff:
    """The user holds the browser. The web app builds the live-view link from `handoff_id` (#14)."""

    handoff_id: HandoffId
    run_id: RunId
    user_id: UserId
    reason: str
    """Why the agent asked, as it would read to the user: "Please sign in to Walmart"."""


@dataclass(frozen=True, kw_only=True)
class HandoffEnded:
    handoff_id: HandoffId
    url: str
    """The page the user left the browser on."""
    saved: bool
    """Whether the state was saved to the jar. False only when the engine raises `NotSupported('export')`."""


class UnknownRun(BrowserError):
    """No browser for this run and user: never started, closed, or another user's run."""


class HandoffActive(BrowserError):
    """The user holds the browser, so a call without the active hand-off's id is refused."""


class HandoffNotActive(BrowserError):
    """The call named a hand-off that is not the run's active one: it ended, or never existed."""


class BrowserService(Protocol):
    """One browser per run, kept across pauses, with its state saved to the user's jar.

    Errors from the backend (`NotSupported`, `TargetNotFound`, `ActionFailed`) pass through unchanged.
    """

    async def start(self, *, run_id: RunId, user_id: UserId) -> Started:
        """Give the run its browser, started from the user's saved state. Idempotent: a retry of the same run gets
        the same browser back."""
        ...

    async def act(
        self, *, run_id: RunId, user_id: UserId, action: Action, handoff_id: HandoffId | None = None
    ) -> ActionResult:
        """Perform one action on the run's browser.

        The agent passes no `handoff_id`. The live view passes the active hand-off's id while the user drives. A call
        that does not match the hand-off state raises `HandoffActive` or `HandoffNotActive`. If the browser was closed
        since the last call, it is restarted from the saved state first and the result says so.
        """
        ...

    async def snapshot(self, *, run_id: RunId, user_id: UserId) -> SnapshotResult:
        """The agent's view of the page. Refused with `HandoffActive` while the user drives."""
        ...

    async def screenshot(
        self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId | None = None
    ) -> ScreenshotResult:
        """The viewport. The same `handoff_id` rule as `act`, so no screenshot reaches the model while the user
        drives."""
        ...

    async def start_handoff(self, *, run_id: RunId, user_id: UserId, reason: str) -> Handoff:
        """Hand the browser to the user. From now on only calls with this hand-off's id may act.

        Idempotent: if the run already has an active hand-off, that one is returned.
        """
        ...

    async def end_handoff(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> HandoffEnded:
        """Give the browser back to the agent and save the state. A second call raises `HandoffNotActive`."""
        ...

    async def save_state(self, *, run_id: RunId, user_id: UserId) -> None:
        """Save the browser's state to the user's jar now, without closing it. Allowed during a hand-off. Raises
        `NotSupported('export')` for an engine that cannot export."""
        ...

    async def close(self, *, run_id: RunId, user_id: UserId) -> bool:
        """Save the state, then close the browser. Ends any active hand-off. Returns whether the state was saved:
        False only when the engine raises `NotSupported('export')`, and then the jar is left as it was. Later calls
        for the run raise `UnknownRun`."""
        ...
