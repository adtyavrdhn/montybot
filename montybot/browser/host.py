"""`BrowserHost`: the browser service. It owns every browser and implements `BrowserService` over any backend.

- **One browser per run.** `start` makes a backend with `new_backend()` and opens it from the user's saved state. A
  retry of the same run gets the same browser back, so a browser outlives the agent attempt that started it.
- **One run per user.** A run holds the user's `JarLease` from `start` until `close`. Another run of the same user
  gets `UserBusy` until then, so two runs never save over each other.
- **Saving.** `save_state`, `end_handoff`, `close` and the idle reaper export the browser's state into the user's
  `SignInJar`. Call `save_state` before pausing a run, so a crash during the pause loses nothing.
- **Idle reaper.** Inside `async with BrowserHost(...)`, a task saves and closes every browser that has not been used
  for `idle_timeout` seconds. The run stays alive: its next call starts a new browser.
- **Restarts are reported.** When a run's browser is gone (reaped, crashed, or the service restarted), the next call
  starts a new one from the saved state, and its result carries `Restarted(reason, url)`. A crash is noticed when a
  call fails and the browser no longer answers `snapshot()`. A read (`snapshot`, `screenshot`) is then retried on the
  new browser; an action is not, because the page it was aimed at is gone, so it raises `ActionFailed`.
- **After a service restart,** a run is found again through its lease: if the run still holds the user's lease, its
  next call restarts the browser. Active hand-offs are not kept across a restart.

The wire is in-process: callers hold a `BrowserHost` (or anything typed as `BrowserService`). Every argument and
result is a plain dataclass, so an HTTP or socket wire can be added in front of it without changing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self, TypeVar

from montybot.browser.contract import (
    Action,
    ActionFailed,
    BrowserBackend,
    Download,
    DownloadsBackend,
    NotSupported,
    Screenshot,
    TargetNotFound,
)
from montybot.browser.jar import JarLease, SignInJar
from montybot.browser.live import FrameSource, LiveViewBackend
from montybot.browser.service import (
    ActionResult,
    Handoff,
    HandoffActive,
    HandoffEnded,
    HandoffId,
    HandoffNotActive,
    Restarted,
    RunId,
    ScreenshotResult,
    SnapshotResult,
    Started,
    UnknownRun,
    UserBusy,
    UserId,
)
from montybot.browser.state import BLANK_URL, BrowserState
from montybot.liveview.polling import PollingFrameSource

T = TypeVar('T')

BackendFactory = Callable[[], BrowserBackend]
"""Makes a closed backend for one run. The host opens it, and closes it when the run is done with it."""

DEFAULT_IDLE_TIMEOUT = 10 * 60.0
"""Seconds without a call before the reaper saves and closes a browser."""

CRASHED = 'the browser stopped unexpectedly'
SERVICE_RESTARTED = 'the browser service restarted'
_STOPPED_DURING_CALL = (
    'The browser stopped during this call, so it did not finish. '
    'The next call starts it again from the saved sign-ins and says where it is.'
)
_NOT_SAVED = '; changes since the last save are lost, because this browser cannot export its state'

logger = logging.getLogger(__name__)


@dataclass(kw_only=True, eq=False)
class _Run:
    run_id: RunId
    user_id: UserId
    backend: BrowserBackend | None = None
    """None before the first call, and after the browser was reaped or crashed."""
    restart_reason: str | None = None
    """Set when the browser was closed under the run: the next open reports it as `Restarted`."""
    restarted: Restarted | None = None
    """A restart the caller has not been told about yet."""
    handoff: Handoff | None = None
    url: str = BLANK_URL
    """The last URL the host saw: from the saved state, a snapshot or an export."""
    saved: bool = True
    """Whether the jar holds the browser's state as of the last save. False after an engine could not export it."""
    last_used: float = 0.0
    closed: bool = False
    """Set by `close`, for calls that were waiting for the lock meanwhile."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held for every call into the backend, which is not safe to use concurrently."""
    sources: list[FrameSource] = field(default_factory=list[FrameSource])
    """Live views of the active hand-off, closed when it ends or the browser closes."""


class _Gone(Exception):
    """The run's browser stopped answering. It has been closed and marked for a restart."""


class BrowserHost:
    """The browser service, in-process. See the module docstring.

    `idle_timeout` is in seconds. `reap_every` is how often the reaper looks, by default a quarter of `idle_timeout`
    and at most a minute.
    """

    def __init__(
        self,
        *,
        new_backend: BackendFactory,
        jar: SignInJar,
        lease: JarLease,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        reap_every: float | None = None,
    ) -> None:
        self._new_backend = new_backend
        self._jar = jar
        self._lease = lease
        self.idle_timeout = idle_timeout
        self._reap_every = reap_every if reap_every is not None else min(idle_timeout / 4, 60.0)
        self._runs: dict[RunId, _Run] = {}
        self._closed: set[RunId] = set()
        self._reaper: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._reaper = asyncio.create_task(self._reap_forever())
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop the reaper, then save and close every browser. Runs keep their leases, so if the service starts again
        with the same lease store, each run's next call restarts its browser and says so."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for run in list(self._runs.values()):
            async with run.lock:
                await self._save_and_drop(run, SERVICE_RESTARTED)

    # --- BrowserService ---

    async def start(self, *, run_id: RunId, user_id: UserId) -> Started:
        run = self._runs.get(run_id)
        if run is None:
            run = await self._claim(run_id, user_id)
        elif run.user_id != user_id:
            raise UnknownRun('no browser for this run')
        async with self._hold(run):
            before = run.backend
            url, restarted = await self._use(run, self._url_now(run), replay=True)
            reused = before is not None and run.backend is before
            return Started(url=url, reused=reused, restarted=restarted)

    async def act(
        self, *, run_id: RunId, user_id: UserId, action: Action, handoff_id: HandoffId | None = None
    ) -> ActionResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, handoff_id)
            _, restarted = await self._use(run, lambda backend: backend.act(action), replay=False)
            return ActionResult(restarted=restarted)

    async def snapshot(self, *, run_id: RunId, user_id: UserId) -> SnapshotResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, None)
            snapshot, restarted = await self._use(run, lambda backend: backend.snapshot(), replay=True)
            run.url = snapshot.url
            return SnapshotResult(snapshot=snapshot, restarted=restarted)

    async def screenshot(
        self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId | None = None
    ) -> ScreenshotResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, handoff_id)
            screenshot, restarted = await self._use(run, lambda backend: backend.screenshot(), replay=True)
            return ScreenshotResult(screenshot=screenshot, restarted=restarted)

    async def peek_screenshot(self, *, run_id: RunId, user_id: UserId) -> Screenshot:
        """The viewport of the run's open browser, for the user watching the run. Read only: it never opens, restarts
        or keeps a browser alive, and does not wait for a call in progress. `UnknownRun` if there is no open browser
        or it is busy; `HandoffActive` during a hand-off."""
        run = self._runs.get(run_id)
        backend = run.backend if run is not None and run.user_id == user_id else None
        if run is None or backend is None or run.lock.locked():
            raise UnknownRun('no browser to watch for this run')
        async with run.lock:  # free when checked above; only a waiter woken just before could get it first
            _check_handoff(run, None)
            try:
                return await self._call(run, backend, lambda backend: backend.screenshot())
            except Exception as error:  # a watcher gets the next frame instead, whatever went wrong with this one
                raise UnknownRun('no browser to watch for this run') from error

    async def start_handoff(self, *, run_id: RunId, user_id: UserId, reason: str) -> Handoff:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            if run.handoff is None:
                run.handoff = Handoff(
                    handoff_id=secrets.token_urlsafe(16), run_id=run_id, user_id=user_id, reason=reason
                )
            _touch(run)
            return run.handoff

    async def end_handoff(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> HandoffEnded:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            if run.handoff is None or not secrets.compare_digest(run.handoff.handoff_id, handoff_id):
                raise HandoffNotActive('that hand-off is not active')
            await _close_sources(run)
            saved = await self._save_if_open(run)
            if not saved and run.backend is not None:  # a save would have read the URL
                with contextlib.suppress(_Gone):
                    await self._call(run, run.backend, self._url_now(run))
            run.handoff = None
            _touch(run)
            return HandoffEnded(handoff_id=handoff_id, url=run.url, saved=saved)

    async def live_view(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> FrameSource:
        """A live view of the run's browser for the active hand-off's holder: the backend's own (a CDP screencast for
        Chromium), else one that polls `screenshot()`. Added for the web app (#8), on #14's `FrameSource`."""
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, handoff_id)
            backend = await self._open(run)
            if isinstance(backend, LiveViewBackend):
                source = await backend.live_view()
            else:
                source = await PollingFrameSource.start(backend)
            run.sources.append(source)
            _touch(run)
            return source

    async def save_state(self, *, run_id: RunId, user_id: UserId) -> None:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            backend = run.backend
            if backend is None:
                return  # reaped (and saved then) or crashed (nothing left to save); the next call restarts it
            try:
                state = await self._call(run, backend, lambda backend: backend.export())
            except NotSupported as error:
                if error.feature == 'export':
                    run.saved = False
                raise
            except _Gone as gone:
                raise ActionFailed(_STOPPED_DURING_CALL) from gone
            await self._store(run, state)
            _touch(run)

    async def take_downloads(self, *, run_id: RunId, user_id: UserId) -> list[Download]:
        """Added for the run's files (#21). Allowed during a hand-off, so what the user downloads is kept too."""
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            if not isinstance(run.backend, DownloadsBackend):
                return []
            return await run.backend.take_downloads()

    async def close(self, *, run_id: RunId, user_id: UserId) -> bool:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            try:
                saved = await self._save_if_open(run)
            except UserBusy:  # the lease was lost, so the jar belongs to another run now
                saved = False
            finally:
                await self._drop(run)
                run.handoff = None
                run.closed = True
                self._runs.pop(run_id, None)
                self._closed.add(run_id)
                await self._lease.release(user_id=user_id, run_id=run_id)
            return saved

    # --- the idle reaper ---

    async def reap_idle(self) -> int:
        """Save and close every browser not used for `idle_timeout` seconds. Returns how many it closed."""
        reaped = 0
        for run in list(self._runs.values()):
            if run.lock.locked() or not self._has_idle_browser(run):
                continue
            async with run.lock:
                if not self._has_idle_browser(run):
                    continue  # used or closed while the reaper waited for the lock
                await self._save_and_drop(run, f'closed after {_duration(self.idle_timeout)} idle')
                reaped += 1
        return reaped

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(self._reap_every)
            await self.reap_idle()

    def _has_idle_browser(self, run: _Run) -> bool:
        return run.backend is not None and time.monotonic() - run.last_used >= self.idle_timeout

    # --- runs ---

    async def _claim(self, run_id: RunId, user_id: UserId) -> _Run:
        """Make the record for a run that `start` has not seen, taking the user's lease."""
        if run_id in self._closed:
            raise UnknownRun('no browser for this run')
        held_already = await self._lease.holder(user_id=user_id) == run_id
        if not await self._lease.acquire(user_id=user_id, run_id=run_id):
            raise UserBusy("another run of this user's is using the browser and the saved sign-ins")
        run = self._runs.get(run_id)
        if run is None:  # no await since the lookup, so a concurrent `start` cannot have made one meanwhile
            restart_reason = SERVICE_RESTARTED if held_already else None
            run = self._runs[run_id] = _Run(run_id=run_id, user_id=user_id, restart_reason=restart_reason)
        elif run.user_id != user_id:
            await self._lease.release(user_id=user_id, run_id=run_id)
            raise UnknownRun('no browser for this run')
        return run

    async def _find(self, run_id: RunId, user_id: UserId) -> _Run:
        """The run's record, or `UnknownRun`. A run this process has not seen, whose lease is still held, belonged to
        an earlier process: it is taken over and its browser restarted on first use."""
        run = self._runs.get(run_id)
        if run is None and run_id not in self._closed and await self._lease.holder(user_id=user_id) == run_id:
            run = self._runs.setdefault(run_id, _Run(run_id=run_id, user_id=user_id, restart_reason=SERVICE_RESTARTED))
        if run is None or run.user_id != user_id:
            raise UnknownRun('no browser for this run')
        return run

    @contextlib.asynccontextmanager
    async def _hold(self, run: _Run) -> AsyncGenerator[None]:
        """Hold the run's lock, for one call at a time into its backend. A run closed while waiting is unknown."""
        async with run.lock:
            if run.closed:
                raise UnknownRun('no browser for this run')
            yield

    # --- the browser of a run; the caller holds `run.lock` ---

    async def _use(
        self, run: _Run, use: Callable[[BrowserBackend], Awaitable[T]], *, replay: bool
    ) -> tuple[T, Restarted | None]:
        """Call `use` on the run's browser, opening it first if it is not open. If the browser stops during the call,
        retry once on a new one when `replay` is set, else raise `ActionFailed`. Returns the restart to report."""
        attempts = 2 if replay else 1
        for attempt in range(1, attempts + 1):
            backend = await self._open(run)
            try:
                result = await self._call(run, backend, use)
            except _Gone as gone:
                if attempt < attempts:
                    continue
                raise ActionFailed(_STOPPED_DURING_CALL) from gone
            _touch(run)
            restarted, run.restarted = run.restarted, None
            return result, restarted
        raise AssertionError('unreachable')

    async def _open(self, run: _Run) -> BrowserBackend:
        if run.backend is not None:
            return run.backend
        state = await self._jar.load(user_id=run.user_id)
        backend = self._new_backend()
        try:
            await backend.open(state)
        except ActionFailed:
            pass  # the saved page did not load; the browser is open anyway, and the next snapshot shows the error
        except BaseException:
            await _close_quietly(backend)
            raise
        run.backend = backend
        run.url = state.url if state is not None else BLANK_URL
        run.saved = True
        if run.restart_reason is not None:
            run.restarted = Restarted(reason=run.restart_reason, url=run.url)
            run.restart_reason = None
        return backend

    async def _call(self, run: _Run, backend: BrowserBackend, use: Callable[[BrowserBackend], Awaitable[T]]) -> T:
        """Call `use`. If it fails and the browser no longer answers, drop it and raise `_Gone`."""
        try:
            return await use(backend)
        except (NotSupported, TargetNotFound):
            raise
        except Exception as error:
            if await _answers(backend):
                raise
            await self._drop(run, CRASHED)
            raise _Gone from error

    def _url_now(self, run: _Run) -> Callable[[BrowserBackend], Awaitable[str]]:
        async def url_now(backend: BrowserBackend) -> str:
            with contextlib.suppress(NotSupported):
                run.url = (await backend.snapshot()).url
            return run.url

        return url_now

    async def _save_if_open(self, run: _Run) -> bool:
        """Save the open browser's state. Returns whether the jar now holds the browser's latest state: False if the
        engine cannot export, or the browser was lost before it could."""
        if run.backend is None:
            return run.saved
        try:
            state = await self._call(run, run.backend, lambda backend: backend.export())
        except NotSupported as error:
            if error.feature != 'export':
                raise
            run.saved = False
            return False
        except _Gone:
            run.saved = False
            return False
        await self._store(run, state)
        return True

    async def _store(self, run: _Run, state: BrowserState) -> None:
        if await self._lease.holder(user_id=run.user_id) != run.run_id:
            raise UserBusy("this run no longer holds the user's saved sign-ins, so it cannot save them")
        await self._jar.save(user_id=run.user_id, state=state)
        run.url = state.url
        run.saved = True

    async def _save_and_drop(self, run: _Run, reason: str) -> None:
        """Save and close the run's browser, keeping the run, for the reaper and shutdown. Never raises a backend
        error: the browser is closed either way."""
        try:
            saved = await self._save_if_open(run)
        except Exception as error:  # noqa: BLE001  a broken export must not keep a browser alive
            logger.warning('could not save run %s before closing it: %s', run.run_id, type(error).__name__)
            saved = False
        if run.backend is not None:
            await self._drop(run, reason if saved else reason + _NOT_SAVED)

    async def _drop(self, run: _Run, reason: str | None = None) -> None:
        """Close the run's browser. With a `reason`, the next call restarts it and reports why."""
        await _close_sources(run)
        backend, run.backend = run.backend, None
        if backend is not None:
            await _close_quietly(backend)
        run.restart_reason = reason


def _check_handoff(run: _Run, handoff_id: HandoffId | None) -> None:
    """Only the active hand-off's holder may use the browser while one is active; with none, nobody may claim one."""
    if handoff_id is None:
        if run.handoff is not None:
            raise HandoffActive('the user is using the browser; wait until they hand it back')
    elif run.handoff is None or not secrets.compare_digest(run.handoff.handoff_id, handoff_id):
        raise HandoffNotActive('that hand-off is not active')


async def _close_sources(run: _Run) -> None:
    sources, run.sources = run.sources, []
    for source in sources:
        with contextlib.suppress(Exception):
            await source.close()


def _touch(run: _Run) -> None:
    run.last_used = time.monotonic()


async def _answers(backend: BrowserBackend) -> bool:
    """Whether the browser is still alive: it answers `snapshot()`, or says it cannot."""
    try:
        await backend.snapshot()
    except NotSupported:
        return True
    except Exception:  # noqa: BLE001  a dead engine may raise anything
        return False
    return True


async def _close_quietly(backend: BrowserBackend) -> None:
    """Close a backend that may already be dead. `close()` is safe in any state, but a crashed engine may still
    raise."""
    with contextlib.suppress(Exception):
        await backend.close()


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f'{minutes} minute' + ('s' if minutes != 1 else '')
    return f'{seconds:g} seconds'
