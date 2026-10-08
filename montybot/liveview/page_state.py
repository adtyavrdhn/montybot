"""`PageState`: the active tab's loading state and history, and a browser's buttons for it, over CDP.

Both Chromium sources use it, on their own CDP session for the active tab: `CdpFrameSource` (Playwright,
`chromium.py`) and `CDPFrameSource` (our pipe, `cdp.py`). It enables the `Page` domain on that session only, so the
run's own session is never touched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import cast

from montybot.browser.cdp_client import CDPParams
from montybot.browser.contract import ActionFailed
from montybot.browser.live import PageCommand, Tab

CDPSend = Callable[[str, CDPParams | None], Awaitable[CDPParams]]
"""Send one command on the active tab's session and return its result."""

EVENTS = (
    'Page.frameStartedLoading',
    'Page.frameStoppedLoading',
    'Page.frameNavigated',
    'Page.navigatedWithinDocument',
)
"""The events to pass to `PageState.handler`, subscribed before `start()`."""


async def _closed(method: str, params: CDPParams | None) -> CDPParams:
    raise ActionFailed('the live view is closed')


class PageState:
    """What a browser's toolbar shows for one tab: whether it is loading, and whether it can go back or forward.

    `changed` is called whenever any of that may have changed; `spawn` runs a history read in the background.
    """

    def __init__(
        self,
        send: CDPSend,
        *,
        changed: Callable[[], None],
        spawn: Callable[[Coroutine[object, object, None]], None],
    ) -> None:
        self._send = send
        self._changed = changed
        self._spawn = spawn
        self._frame_id = ''
        self.loading = False
        self.can_go_back = False
        self.can_go_forward = False

    @classmethod
    def none(cls) -> PageState:
        """For no active session: nothing loading, no history."""
        return cls(_closed, changed=lambda: None, spawn=lambda coroutine: coroutine.close())

    async def start(self) -> None:
        tree = await self._send('Page.getFrameTree', None)
        self._frame_id = str(cast(CDPParams, tree['frameTree'])['frame']['id'])
        await self._send('Page.enable', None)
        await self._read_history()

    def handler(self, event: str) -> Callable[[CDPParams], None]:
        """The listener for one of `EVENTS`. Only the tab's main frame counts, as in a browser's toolbar."""

        def on_event(params: CDPParams) -> None:
            frame = cast(CDPParams, params.get('frame', {})).get('id') if 'frame' in params else params.get('frameId')
            if frame != self._frame_id:
                return
            if event == 'Page.frameStartedLoading':
                self.loading = True
                self._changed()
                return
            if event == 'Page.frameStoppedLoading':
                self.loading = False
                self._changed()
            self._spawn(self._read_history())

        return on_event

    async def command(self, command: PageCommand) -> None:
        match command:
            case 'back' | 'forward':
                history = await self._send('Page.getNavigationHistory', None)
                entries = cast(list[CDPParams], history['entries'])
                index = int(history['currentIndex']) + (-1 if command == 'back' else 1)
                if not 0 <= index < len(entries):
                    raise ActionFailed(f'nothing to go {command} to')
                await self._send('Page.navigateToHistoryEntry', {'entryId': entries[index]['id']})
            case 'reload':
                await self._send('Page.reload', None)
            case 'stop':
                await self._send('Page.stopLoading', None)

    def tab(self, *, tab_id: str, url: str, title: str, active: bool, closable: bool) -> Tab:
        """A tab as the user sees it; only the active one, whose page this is, shows loading and history."""
        return Tab(
            tab_id=tab_id,
            url=url,
            title=title,
            active=active,
            closable=closable,
            loading=active and self.loading,
            can_go_back=active and self.can_go_back,
            can_go_forward=active and self.can_go_forward,
        )

    async def _read_history(self) -> None:
        history = await self._send('Page.getNavigationHistory', None)
        index, count = int(history['currentIndex']), len(cast(list[CDPParams], history['entries']))
        self.can_go_back, self.can_go_forward = index > 0, index < count - 1
        self._changed()
