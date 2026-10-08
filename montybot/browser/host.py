"""`BrowserHost`: the browser service. It owns every browser and implements `BrowserService` over any backend.

- **One browser per run, or one tab per run.** `start` makes a backend with `new_backend()` and opens it from the
  user's saved state. With `share_browser`, a run of a user whose browser is open gets a tab of that browser instead
  (`TabsBackend.new_tab`), sharing its cookies, so runs of one user work side by side. A retry of the same run gets
  the same browser or tab back, so it outlives the agent attempt that started it. Only runs going out the same way
  (`Detour`: the user's Mac or the server) share a browser.
- **Kept open between runs.** With `keep_open`, a user's browser stays open when their last run ends (`close` still
  saves it and frees the lease), and their next run carries on in it where the last one left off: open tabs, forms
  and all. Closed only once idle for `idle_timeout`, to make room (`max_open_browsers`), or when it cannot export.
- **One browser per user.** A run holds the user's `JarLease` from `start` until `close`. Without `share_browser`,
  another run of the same user gets `UserBusy` until then, so two browsers never save over each other. With it, runs
  share the lease, and the one browser they share is the only writer.
- **Saving.** `save_state`, `end_handoff`, `close` and the idle reaper export the browser's state into the user's
  `SignInJar`. Call `save_state` before pausing a run, so a crash during the pause loses nothing.
- **Idle reaper.** Inside `async with BrowserHost(...)`, a task saves and closes every browser that has not been used
  for `idle_timeout` seconds (a day by default), parked ones too. A run stays alive: its next call starts a new
  browser.
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
from pathlib import Path
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
    TabsBackend,
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
from montybot.observability import timed, timing

T = TypeVar('T')

BackendFactory = Callable[[], BrowserBackend]
"""Makes a closed backend for one run. The host opens it, and closes it when the run is done with it."""


@dataclass(frozen=True, kw_only=True)
class Detour:
    """Some browsers leave through another egress proxy (the Mac tunnel, `tunnel.py`). Each time the host launches a
    browser it asks `route` for the run's proxy socket; None is the usual way, `new_backend` makes one for a socket.
    A run that gets a tab of its user's open browser shares that browser's way out."""

    route: Callable[[RunId, UserId], Awaitable[Path | None]]
    new_backend: Callable[[Path], BrowserBackend]


DEFAULT_IDLE_TIMEOUT = 24 * 60 * 60.0
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
    egress: Path | None = None
    """The way out the run's browser goes (`Detour`); None is the usual one. Only runs going out the same way share a
    browser."""


@dataclass(kw_only=True, eq=False)
class _Parked:
    """A user's browser, kept open after their last run ended (`keep_open`): that run's tab, where it left off."""

    backend: BrowserBackend
    egress: Path | None
    url: str
    since: float = field(default_factory=time.monotonic)


class _Gone(Exception):
    """The run's browser stopped answering. It has been closed and marked for a restart."""


class BrowserHost:
    """The browser service, in-process. See the module docstring.

    `idle_timeout` is in seconds. `reap_every` is how often the reaper looks, by default a quarter of `idle_timeout`
    and at most a minute. `share_browser` (for an engine with tabs, `TabsBackend`) gives runs of the same user tabs
    of one browser, so they run side by side; `max_open_browsers` then counts each user's browser once.
    `keep_open` keeps a user's browser open when their last run ends, for their next run, until it has been idle for
    `idle_timeout` or must make room for another.
    """

    def __init__(
        self,
        *,
        new_backend: BackendFactory,
        jar: SignInJar,
        lease: JarLease,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        reap_every: float | None = None,
        max_open_browsers: int | None = None,
        share_browser: bool = False,
        detour: Detour | None = None,
        keep_open: bool = False,
    ) -> None:
        self._new_backend = new_backend
        self._detour = detour
        self._keep_open = keep_open
        self._parked: dict[UserId, _Parked] = {}
        self._share_browser = share_browser
        self._user_locks: dict[UserId, asyncio.Lock] = {}
        self._jar = jar
        self._lease = lease
        self.idle_timeout = idle_timeout
        self._reap_every = reap_every if reap_every is not None else min(idle_timeout / 4, 60.0)
        if max_open_browsers is not None and max_open_browsers < 1:
            raise ValueError('max_open_browsers must be positive')
        self._max_open_browsers = max_open_browsers
        self._admission = asyncio.Lock()
        self._launching = 0
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

    @timed('browser.shutdown')
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
        parked, self._parked = self._parked, {}
        for browser in parked.values():
            await _close_quietly(browser.backend)

    # --- BrowserService ---

    @timed('browser.start')
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

    @timed('browser.act')
    async def act(
        self, *, run_id: RunId, user_id: UserId, action: Action, handoff_id: HandoffId | None = None
    ) -> ActionResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, handoff_id)
            _, restarted = await self._use(run, lambda backend: backend.act(action), replay=False)
            return ActionResult(restarted=restarted)

    @timed('browser.snapshot')
    async def snapshot(self, *, run_id: RunId, user_id: UserId) -> SnapshotResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, None)
            snapshot, restarted = await self._use(run, lambda backend: backend.snapshot(), replay=True)
            run.url = snapshot.url
            return SnapshotResult(snapshot=snapshot, restarted=restarted)

    @timed('browser.screenshot')
    async def screenshot(
        self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId | None = None
    ) -> ScreenshotResult:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            _check_handoff(run, handoff_id)
            screenshot, restarted = await self._use(run, lambda backend: backend.screenshot(), replay=True)
            return ScreenshotResult(screenshot=screenshot, restarted=restarted)

    @timed('browser.peek_screenshot', only_in_trace=True)
    async def peek_screenshot(self, *, run_id: RunId, user_id: UserId) -> Screenshot:
        """The viewport of the run's open browser, for the user watching the run. Read only: it never opens, restarts
        or keeps a browser alive, and does not wait for a call in progress. Once the run has ended, the user's browser
        kept open (`keep_open`), where their last run left it. `UnknownRun` if there is no open browser or it is busy;
        `HandoffActive` during a hand-off."""
        run = self._runs.get(run_id)
        if run is None:
            return await self._peek_parked(user_id)
        backend = run.backend if run.user_id == user_id else None
        if backend is None or run.lock.locked():
            raise UnknownRun('no browser to watch for this run')
        async with run.lock:  # free when checked above; only a waiter woken just before could get it first
            _check_handoff(run, None)
            try:
                return await self._call(run, backend, lambda backend: backend.screenshot())
            except Exception as error:  # a watcher gets the next frame instead, whatever went wrong with this one
                raise UnknownRun('no browser to watch for this run') from error

    async def _peek_parked(self, user_id: UserId) -> Screenshot:
        """The viewport of the user's parked browser. Under the user's lock, so a run cannot take it meanwhile; never
        waits for that lock."""
        lock = self._user_locks.get(user_id)
        if user_id not in self._parked or (lock is not None and lock.locked()):
            raise UnknownRun('no browser to watch for this run')
        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
            parked = self._parked.get(user_id)
            if parked is None:
                raise UnknownRun('no browser to watch for this run')
            try:
                return await parked.backend.screenshot()
            except Exception as error:  # stopped while parked: the next run finds out and starts another
                raise UnknownRun('no browser to watch for this run') from error

    @timed('browser.handoff.start')
    async def start_handoff(self, *, run_id: RunId, user_id: UserId, reason: str) -> Handoff:
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            if run.handoff is None:
                run.handoff = Handoff(
                    handoff_id=secrets.token_urlsafe(16), run_id=run_id, user_id=user_id, reason=reason
                )
            _touch(run)
            return run.handoff

    @timed('browser.handoff.end')
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

    @timed('browser.live_view')
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

    @timed('browser.state.save')
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

    @timed('browser.downloads')
    async def take_downloads(self, *, run_id: RunId, user_id: UserId) -> list[Download]:
        """Added for the run's files (#21). Allowed during a hand-off, so what the user downloads is kept too."""
        run = await self._find(run_id, user_id)
        async with self._hold(run):
            if not isinstance(run.backend, DownloadsBackend):
                return []
            return await run.backend.take_downloads()

    @timed('browser.close')
    async def close(self, *, run_id: RunId, user_id: UserId) -> bool:
        try:
            run = await self._find(run_id, user_id)
        except UnknownRun:
            # The run may be stopped before its first browser call: one already on its way must not open a browser.
            self._closed.add(run_id)
            raise
        async with self._hold(run):
            saved = False
            try:
                saved = await self._save_if_open(run)
            except UserBusy:  # the lease was lost, so the jar belongs to another run now
                pass
            finally:
                if saved and self._keep_open:
                    await self._park(run)
                await self._drop(run)
                run.handoff = None
                run.closed = True
                self._runs.pop(run_id, None)
                self._closed.add(run_id)
                await self._lease.release(user_id=user_id, run_id=run_id)
            return saved

    # --- the idle reaper ---

    @timed('browser.reap_idle')
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
        for user_id, browser in list(self._parked.items()):
            if time.monotonic() - browser.since >= self.idle_timeout and self._parked.get(user_id) is browser:
                del self._parked[user_id]
                await _close_quietly(browser.backend)
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
        held_already = await self._lease.holds(user_id=user_id, run_id=run_id)
        if not await self._lease.acquire(user_id=user_id, run_id=run_id, shared=self._share_browser):
            raise UserBusy("another run of this user's is using the browser and the saved sign-ins")
        if run_id in self._closed:  # closed while this call waited for the lease
            await self._lease.release(user_id=user_id, run_id=run_id)
            raise UnknownRun('no browser for this run')
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
        if run is None and run_id not in self._closed and await self._lease.holds(user_id=user_id, run_id=run_id):
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
        # One at a time per user, so a run opening a tab sees the browser another run of the user has just started.
        async with self._user_locks.setdefault(run.user_id, asyncio.Lock()):
            if self._detour is not None:
                run.egress = await self._detour.route(run.run_id, run.user_id)
            if (sibling := self._sibling(run)) is not None and (tab := await self._open_tab(run, sibling)) is not None:
                return tab
            if (parked := await self._unpark(run)) is not None:
                return parked
            return await self._launch(run)

    async def _park(self, run: _Run) -> None:
        """Keep the ending run's browser open for the user's next run. Not while another run of the user has a tab in
        it: then only this run's tab closes. A user has one parked browser; an older one is closed."""
        await _close_sources(run)
        async with self._user_locks.setdefault(run.user_id, asyncio.Lock()):
            if run.backend is None or (self._share_browser and self._sibling(run) is not None):
                return
            older = self._parked.pop(run.user_id, None)
            self._parked[run.user_id] = _Parked(backend=run.backend, egress=run.egress, url=run.url)
            run.backend = None
        if older is not None:
            await _close_quietly(older.backend)

    async def _unpark(self, run: _Run) -> BrowserBackend | None:
        """The user's parked browser for the run, where it was left, if it goes out the run's way and still answers.
        Otherwise it is closed: its state is in the jar already, which the run's new browser starts from."""
        parked = self._parked.pop(run.user_id, None)
        if parked is None:
            return None
        if parked.egress == run.egress and await _answers(parked.backend):
            return self._opened(run, parked.backend, parked.url)
        await _close_quietly(parked.backend)
        return None

    def _sibling(self, run: _Run) -> TabsBackend | None:
        """The open browser of another run of the same user, to open this run's tab in."""
        if not self._share_browser:
            return None
        for other in self._runs.values():
            if (
                other is not run
                and other.user_id == run.user_id
                and other.egress == run.egress
                and isinstance(other.backend, TabsBackend)
            ):
                return other.backend
        return None

    async def _open_tab(self, run: _Run, sibling: TabsBackend) -> BrowserBackend | None:
        """Open the run's tab in the user's browser, at the run's last page. None if the browser could not give one:
        it has stopped, and the run starts a new one."""
        backend = sibling.new_tab()
        try:
            with timing('browser.open_tab'):
                await backend.open(BrowserState(url=run.url))
        except ActionFailed:
            if not await _answers(backend):
                await _close_quietly(backend)
                return None
            # The page did not load; the tab is open anyway, and the next snapshot shows the error.
        except BaseException:
            await _close_quietly(backend)
            raise
        return self._opened(run, backend, run.url)

    async def _launch(self, run: _Run) -> BrowserBackend:
        """Start a browser for the run from the user's saved state."""
        if self._max_open_browsers is not None:
            async with self._admission:
                await self._make_room(run)
                self._launching += 1  # Reserve without serialising slow page loads across runs.
        try:
            with timing('browser.state.load'):
                state = await self._jar.load(user_id=run.user_id)
            backend = await self._make_backend(run)
            if self._share_browser and not isinstance(backend, TabsBackend):
                raise TypeError('share_browser needs an engine with tabs (TabsBackend)')
            try:
                with timing('browser.launch_restore'):
                    await backend.open(state)
            except ActionFailed:
                pass  # the saved page did not load; the browser is open anyway, and the next snapshot shows the error
            except BaseException:
                await _close_quietly(backend)
                raise
            return self._opened(run, backend, state.url if state is not None else BLANK_URL)
        finally:
            if self._max_open_browsers is not None:
                self._launching -= 1

    async def _make_backend(self, run: _Run) -> BrowserBackend:
        if self._detour is not None and run.egress is not None:
            return self._detour.new_backend(run.egress)
        return self._new_backend()

    def _opened(self, run: _Run, backend: BrowserBackend, url: str) -> BrowserBackend:
        run.backend = backend
        run.url = url
        run.saved = True
        if run.restart_reason is not None:
            run.restarted = Restarted(reason=run.restart_reason, url=run.url)
            run.restart_reason = None
        return backend

    async def _make_room(self, run: _Run) -> None:
        """With `max_open_browsers` browsers open, save and close the one used least recently, if none of its runs is
        in a hand-off or a call. A user's tabs are one browser, closed together."""
        browsers: dict[str, list[_Run]] = {}
        for other in self._runs.values():
            if other.backend is not None and other is not run:
                key = f'user {other.user_id}' if self._share_browser else f'run {other.run_id}'
                browsers.setdefault(key, []).append(other)
        if (
            self._max_open_browsers is None
            or len(browsers) + len(self._parked) + self._launching < self._max_open_browsers
        ):
            return
        if self._parked:  # nobody is using those: the one parked longest goes first
            user_id = min(self._parked, key=lambda user_id: self._parked[user_id].since)
            await _close_quietly(self._parked.pop(user_id).backend)
            return
        candidates = sorted(
            (runs for runs in browsers.values() if not any(r.handoff or r.lock.locked() for r in runs)),
            key=lambda runs: max(r.last_used for r in runs),
        )
        for runs in candidates:
            held: list[_Run] = []
            try:
                for other in runs:
                    await asyncio.wait_for(other.lock.acquire(), 0.1)
                    held.append(other)
            except TimeoutError:
                for other in held:
                    other.lock.release()
                continue
            try:
                if all(other.handoff is None for other in runs):
                    # Reuse the idle reaper's save-and-drop path.
                    for other in runs:
                        if other.backend is not None:
                            await self._save_and_drop(other, 'the browser made room for another run')
                    return
            finally:
                for other in held:
                    other.lock.release()
        raise ActionFailed('all browsers are in use; try again when another run finishes')

    @timed('browser.backend.call')
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

    @timed('browser.state.save_if_open')
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

    @timed('browser.state.store')
    async def _store(self, run: _Run, state: BrowserState) -> None:
        if not await self._lease.holds(user_id=run.user_id, run_id=run.run_id):
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

    @timed('browser.drop')
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


@timed('browser.health')
async def _answers(backend: BrowserBackend) -> bool:
    """Whether the browser is still alive: it answers `snapshot()`, or says it cannot."""
    try:
        await backend.snapshot()
    except NotSupported:
        return True
    except Exception:  # noqa: BLE001  a dead engine may raise anything
        return False
    return True


@timed('browser.backend.close')
async def _close_quietly(backend: BrowserBackend) -> None:
    """Close a backend that may already be dead. `close()` is safe in any state, but a crashed engine may still
    raise."""
    with contextlib.suppress(Exception):
        await backend.close()


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f'{hours} hour' + ('s' if hours != 1 else '')
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f'{minutes} minute' + ('s' if minutes != 1 else '')
    return f'{seconds:g} seconds'
