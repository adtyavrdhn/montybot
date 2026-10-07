"""The one interface every browser engine implements: `BrowserBackend`.

Grown from `AgentBrowser` in `poc/montybot_poc/remote.py`. A backend is one browser with one tab, used by one run at a
time, so its methods are not safe to call concurrently. The browser service (`montybot.browser.service`) owns
backends; the agent and the web app never hold one.

Lifecycle:

    closed --open(state)--> open --release()--> closed      release() returns the state
                            open --close()----> closed      close() discards it
                            open --export()---> open        export() returns the state and keeps going

A closed backend can be opened again. Calling anything but `open` and `close` on a closed backend, or `open` on an open
one, raises `LifecycleError`.

Anything an engine cannot do raises `NotSupported`. A backend never ignores an action, and never returns a state it
knows is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from montybot.browser.state import BrowserState

# --- targets: what an action points at ---


@dataclass(frozen=True, kw_only=True)
class Selector:
    """A CSS selector, such as `#add-eggs`. The first matching element is used."""

    css: str


@dataclass(frozen=True, kw_only=True)
class Ref:
    """A reference the latest `snapshot()` printed, such as `12` for `[12] button "Add to cart"`.

    `montybot.browser.snapshot` defines the format and how long a ref stays valid. A ref the backend cannot resolve
    raises `TargetNotFound`, never a click on something else.
    """

    ref: str


@dataclass(frozen=True, kw_only=True)
class Point:
    """A position in CSS pixels from the top-left corner of the viewport, as in `Screenshot.width` and `height`."""

    x: float
    y: float


ElementTarget = Selector | Ref
Target = Selector | Ref | Point

MouseButton = Literal['left', 'middle', 'right']
Modifier = Literal['Alt', 'Control', 'Meta', 'Shift']

# --- actions: the only things the agent and the user can do to a page ---
#
# `kind` names each action in JSON, so a union of actions decodes without guessing.


@dataclass(frozen=True, kw_only=True)
class Navigate:
    """Load `url`, an absolute URL, in the tab."""

    url: str
    kind: Literal['navigate'] = 'navigate'


@dataclass(frozen=True, kw_only=True)
class Click:
    """A left click on the element, or at the point. Elements are scrolled into view first."""

    target: Target
    kind: Literal['click'] = 'click'


@dataclass(frozen=True, kw_only=True)
class Type:
    """Type `text` as key presses, so the page sees input events.

    With a target: focus that element and replace its value with `text`. Without: type at the focused element's
    caret, as the user's keyboard would.
    """

    text: str
    target: ElementTarget | None = None
    kind: Literal['type'] = 'type'


@dataclass(frozen=True, kw_only=True)
class Press:
    """Press and release one key on the focused element, or the page when nothing is focused.

    `key` is a DOM `KeyboardEvent.key` value: `Enter`, `Tab`, `Escape`, `ArrowDown`, `Backspace`, `a`. The modifiers are
    held down around it.
    """

    key: str
    modifiers: tuple[Modifier, ...] = ()
    kind: Literal['press'] = 'press'


@dataclass(frozen=True, kw_only=True)
class Scroll:
    """A mouse wheel turn of `delta_x`, `delta_y` CSS pixels (positive is right and down), at `at` or the viewport's
    centre."""

    delta_x: float = 0
    delta_y: float = 0
    at: Point | None = None
    kind: Literal['scroll'] = 'scroll'


@dataclass(frozen=True, kw_only=True)
class MouseDown:
    """Move the mouse to `at` and press `button`. With `MouseMove` and `MouseUp`, this is press-and-hold and drag."""

    at: Point
    button: MouseButton = 'left'
    kind: Literal['mouse_down'] = 'mouse_down'


@dataclass(frozen=True, kw_only=True)
class MouseMove:
    """Move the mouse to `at`, keeping any pressed buttons down."""

    at: Point
    kind: Literal['mouse_move'] = 'mouse_move'


@dataclass(frozen=True, kw_only=True)
class MouseUp:
    """Move the mouse to `at` and release `button`."""

    at: Point
    button: MouseButton = 'left'
    kind: Literal['mouse_up'] = 'mouse_up'


Action = Navigate | Click | Type | Press | Scroll | MouseDown | MouseMove | MouseUp

# --- what a backend returns ---


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """The agent's view of the page. `montybot.browser.snapshot` defines the format of `text`."""

    url: str
    title: str
    text: str
    """The page's text with numbered refs for the elements the agent can act on. It always includes the page's visible
    text. Typed passwords are masked."""


@dataclass(frozen=True, kw_only=True)
class Screenshot:
    """The viewport as a PNG."""

    png: bytes
    width: int
    """Viewport width in CSS pixels, the space `Point` uses. The image itself may be larger, by the device pixel
    ratio."""
    height: int
    """Viewport height in CSS pixels."""


MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
"""The largest download a backend keeps (#21)."""


@dataclass(frozen=True, kw_only=True)
class Download:
    """A file a page made the browser download. Added for the run's files (#21)."""

    name: str
    """The file name the site suggested; not safe to use as a path as it is."""
    data: bytes
    too_large: bool = False
    """The file was over `MAX_DOWNLOAD_BYTES`, so it was not kept and `data` is empty."""


# --- errors ---
#
# Messages are safe to show the model: they never contain cookie values, storage values or typed text.

Feature = Literal[
    'navigate',
    'click',
    'type',
    'press',
    'scroll',
    'mouse',
    'selector',
    'ref',
    'point',
    'snapshot',
    'screenshot',
    'export',
    'viewport',
]
"""What `NotSupported` names. Each action is one feature (`mouse` covers down, move and up); `selector`, `ref` and
`point` are the target kinds; `export` covers `export()` and `release()`; `viewport` is the live view's
`FrameSource.set_viewport`."""


def features_of(action: Action) -> tuple[Feature, ...]:
    """The features `action` needs: its own, then its target's kind, if it has a target."""
    match action:
        case Navigate():
            return ('navigate',)
        case Click():
            return ('click', _target_feature(action.target))
        case Type():
            return ('type',) if action.target is None else ('type', _target_feature(action.target))
        case Press():
            return ('press',)
        case Scroll():
            return ('scroll',)
        case MouseDown() | MouseMove() | MouseUp():
            return ('mouse',)


def _target_feature(target: Target) -> Feature:
    match target:
        case Selector():
            return 'selector'
        case Ref():
            return 'ref'
        case Point():
            return 'point'


class BrowserError(Exception):
    """Base class for every error a backend or the browser service raises on purpose."""


class NotSupported(BrowserError):
    """This engine cannot do `feature` at all. It is a property of the engine, so retrying will not help."""

    def __init__(self, feature: Feature, *, engine: str, detail: str = '') -> None:
        self.feature: Feature = feature
        self.engine = engine
        super().__init__(f'{engine} does not support {feature}' + (f': {detail}' if detail else ''))


class LifecycleError(BrowserError):
    """A method was called in the wrong state, such as `act` before `open`, or `open` twice. A bug in the caller."""


class TargetNotFound(BrowserError):
    """No element matched the selector or ref, after the backend's usual short wait."""

    def __init__(self, target: ElementTarget, detail: str = '') -> None:
        self.target = target
        super().__init__(f'nothing matches {target}' + (f': {detail}' if detail else ''))


class ActionFailed(BrowserError):
    """The engine tried and failed, such as a navigation that could not reach the site or a timeout."""


# --- the protocol ---


class BrowserBackend(Protocol):
    """One browser engine running one tab. See the module docstring for the lifecycle."""

    async def open(self, state: BrowserState | None) -> None:
        """Start the browser, seeded with `state`, on `state.url`. `None` starts empty, on `about:blank`.

        Cookies (HttpOnly too) and localStorage are in place before the first page loads. sessionStorage for
        `state.url`'s origin is in place before that page's scripts run, or the page is reloaded once after seeding,
        so its scripts see it. If `state.url` fails to load, this raises `ActionFailed` and the backend stays open.
        """
        ...

    async def export(self) -> BrowserState:
        """The current state, without closing: the URL, every cookie for every domain with HttpOnly ones included,
        localStorage for at least every origin that was seeded or is current, and sessionStorage for the current
        origin only.

        An engine that cannot read all of that, such as one whose cookie export leaves out HttpOnly cookies, raises
        `NotSupported('export')`. It must not fill the gap with the cookies it was seeded with.
        """
        ...

    async def release(self) -> BrowserState:
        """`export()`, then `close()`: hand the session to someone else, so only one side can change it.

        If the export raises, the backend stays open.
        """
        ...

    async def snapshot(self) -> Snapshot:
        """The agent's view of the current page."""
        ...

    async def act(self, action: Action) -> None:
        """Perform `action`. Returns once it is done and any navigation it started has loaded, so a following
        `snapshot()` sees the result. Raises `TargetNotFound`, `ActionFailed` or `NotSupported`."""
        ...

    async def screenshot(self) -> Screenshot:
        """The viewport as it looks now."""
        ...

    async def close(self) -> None:
        """Stop the browser and discard its state. Safe to call in any state, any number of times."""
        ...


@runtime_checkable
class DownloadsBackend(Protocol):
    """Optional, added for the run's files (#21): a backend that keeps what pages download. Without it, a download
    is dropped, as before."""

    async def take_downloads(self) -> list[Download]:
        """The downloads finished since the last call, oldest first. Each is returned once. Downloads not taken when
        the browser closes are lost."""
        ...


@runtime_checkable
class TabsBackend(Protocol):
    """Optional: a browser that can give another run a tab of its own, sharing its cookies and storage. The browser
    service then keeps one browser per user, and runs of the same user work side by side in its tabs."""

    def new_tab(self) -> BrowserBackend:
        """A closed backend for a new tab of this open browser. Its `open(state)` only goes to `state.url`: the
        cookies and storage are the browser's own, already live. Its `export()` reads the whole browser, with its own
        tab's URL. `close()` closes only that tab; the browser stops when its last tab closes."""
        ...
