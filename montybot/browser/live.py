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
from importlib.resources import files
from typing import Any, Literal, Protocol, cast, runtime_checkable

from montybot.browser.contract import (
    BrowserBackend,
    Click,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    Press,
    Scroll,
    Type,
)

LiveInput = Click | Type | Press | Scroll | MouseDown | MouseMove | MouseUp | Navigate
"""What the user can do in the live view: the contract's actions. A `Click` there always targets a `Point`, a `Type`
never has a target (the user types where the page's caret is), and a `Navigate` is the address the user typed into
the address bar, loaded in the active tab. The app checks that address before a source gets it."""


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


OUTLINE_JS: str = files('montybot.browser').joinpath('outline.js').read_text(encoding='utf-8')
"""The page as a screen reader reads it (`outline.js`): a function expression run in the active tab."""


@dataclass(frozen=True, kw_only=True)
class OutlineItem:
    """One thing on screen, in reading order: a heading, a line of text, an image, or a control."""

    role: str
    """`heading`, `text`, `image`, or a control's ARIA role: `button`, `link`, `textbox`, `checkbox` and so on."""
    name: str
    x: float
    y: float
    width: float
    height: float
    """Its box in the viewport's CSS pixels, the space input uses."""
    value: str = ''
    """A field's value; for a password, only how many characters it has."""
    level: int | None = None
    checked: bool | None = None
    disabled: bool = False
    focused: bool = False
    secure: bool = False


@dataclass(frozen=True, kw_only=True)
class Outline:
    """The active tab's visible content for a screen reader. `available` is False for an engine that cannot read
    the page, so the user is told rather than shown nothing."""

    title: str = ''
    items: tuple[OutlineItem, ...] = ()
    available: bool = True

    @classmethod
    def from_walker(cls, result: object) -> Outline:
        """`outline.js`'s result, checked: anything malformed is dropped rather than shown."""
        data = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        items: list[OutlineItem] = []
        for raw in cast(list[Any], data.get('items') or []):
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, Any], raw)
            try:
                items.append(
                    OutlineItem(
                        role=str(item['role']),
                        name=str(item.get('name', '')),
                        x=float(item['x']),
                        y=float(item['y']),
                        width=float(item['width']),
                        height=float(item['height']),
                        value=str(item.get('value') or ''),
                        level=int(item['level']) if item.get('level') is not None else None,
                        checked=bool(item['checked']) if item.get('checked') is not None else None,
                        disabled=item.get('disabled') is True,
                        focused=item.get('focused') is True,
                        secure=item.get('secure') is True,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cls(title=str(data.get('title') or ''), items=tuple(items))

    def to_json(self) -> dict[str, object]:
        return {
            'title': self.title,
            'available': self.available,
            'items': [
                {key: value for key, value in vars(item).items() if not _default(key, value)} for item in self.items
            ],
        }


def _default(key: str, value: object) -> bool:
    """Left out of the JSON: what a reader assumes when it is missing (but an unchecked box still says so)."""
    return value is None or value == '' or (value is False and key in ('disabled', 'focused', 'secure'))


@runtime_checkable
class OutlineSource(Protocol):
    """A `FrameSource` that can also say what is on the page, for a user who drives the live view with a screen
    reader. Chromium's does."""

    async def outline(self) -> Outline: ...


@runtime_checkable
class LiveViewBackend(BrowserBackend, Protocol):
    """A backend with its own `FrameSource`, faster than polling `screenshot()`."""

    async def live_view(self) -> FrameSource:
        """A source for the open browser. Raises `LifecycleError` if it is closed."""
        ...
