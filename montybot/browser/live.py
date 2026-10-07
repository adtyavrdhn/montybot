"""The live view's side of the contract: a stream of the viewport and a way to send the user's input.

Added for the live view (#14). The browser service hands out a `FrameSource` only for the run's active hand-off
(`BrowserService.live_view`), so frames reach the user who holds the browser and never the agent.

A backend that can stream its viewport cheaply (Chromium's screencast, Servo's screenshots) implements
`LiveViewBackend`. For any other backend the service can poll `screenshot()` and pass input to `act()`, which is what
`montybot.liveview.polling.PollingFrameSource` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from montybot.browser.contract import BrowserBackend, Click, MouseDown, MouseMove, MouseUp, Press, Scroll, Type

LiveInput = Click | Type | Press | Scroll | MouseDown | MouseMove | MouseUp
"""What the user can do in the live view: the contract's actions without `Navigate`. A `Click` there always targets a
`Point`, and a `Type` never has a target: the user types where the page's caret is."""


@dataclass(frozen=True, kw_only=True)
class Frame:
    """One picture of the active tab's viewport."""

    image: bytes
    mime: Literal['image/jpeg', 'image/png']
    width: int
    """Viewport width in CSS pixels, the space `Point` uses. The image may be larger or smaller."""
    height: int
    """Viewport height in CSS pixels."""


@dataclass(frozen=True, kw_only=True)
class Viewport:
    """A small screen to lay pages out for, as a phone's browser would: the user's room for the picture."""

    width: int
    """In CSS pixels."""
    height: int
    """In CSS pixels."""


@dataclass(frozen=True, kw_only=True)
class Tab:
    tab_id: str
    url: str
    title: str
    active: bool
    """The tab the frames show and the input goes to."""


@dataclass(frozen=True, kw_only=True)
class Tabs:
    """Every tab the user can see, sent whenever one opens, closes, navigates or becomes active."""

    tabs: tuple[Tab, ...]


class FrameSource(Protocol):
    """The live picture of one browser and the user's input into it, for one live-view connection.

    New tabs and popups are followed as a browser window would: the newest one becomes active. When the active tab
    closes, the run's own tab becomes active again. `close()` goes back to the run's own tab, so the agent carries on
    in it; other tabs the user left open stay open.
    """

    def updates(self) -> AsyncIterator[Frame | Tabs]:
        """The latest frame and tab list, as they change. A slow reader skips frames instead of falling behind.

        Ends when the source is closed, including by the browser service when the hand-off ends.
        """
        ...

    async def send(self, action: LiveInput) -> None:
        """Perform the user's input on the active tab. Raises `NotSupported` or `ActionFailed` like `act()`."""
        ...

    async def switch_tab(self, tab_id: str) -> None:
        """Show `tab_id` and send input to it. An unknown id raises `ActionFailed`."""
        ...

    async def set_viewport(self, viewport: Viewport | None) -> None:
        """Lay the pages out at `viewport`, with a phone's layout, so a user on a phone can read and tap them.
        `None`, and `close()`, give the browser its own size back. An engine that cannot raises
        `NotSupported('viewport')`; the user then sees its normal size, scaled down."""
        ...

    async def close(self) -> None:
        """Stop streaming, release any held mouse button, restore the browser's own size, and go back to the run's
        tab. The browser stays open. Safe to call more than once."""
        ...


@runtime_checkable
class LiveViewBackend(BrowserBackend, Protocol):
    """A backend with its own `FrameSource`, faster than polling `screenshot()`."""

    async def live_view(self) -> FrameSource:
        """A source for the open browser. Raises `LifecycleError` if it is closed."""
        ...
