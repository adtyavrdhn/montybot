"""`FakeBrowser`: an in-memory `BrowserBackend`, so agent-side work never waits on a real engine.

Pages are Python objects keyed by URL. A page's `on_load` and `on_action` hooks play the part of its server and its
scripts, so a test can build a shop with a sign-in, a cart and an HttpOnly session cookie in a few lines:

    def sign_in(browser: FakeBrowser, action: Action) -> None:
        if isinstance(action, Click) and action.target == Selector(css='#submit'):
            browser.cookies.append(Cookie(name='sid', value='s3cret', domain='shop.test', http_only=True))
            browser.load('http://shop.test/account')

    browser = FakeBrowser(pages={
        'http://shop.test/login': FakePage(
            title='Sign in',
            elements=[FakeElement(selector='#submit', role='button', name='Sign in')],
            on_action=sign_in,
        ),
        'http://shop.test/account': FakePage(title='Your account', text='Signed in'),
    })

What the fake does by itself: a click on an element with `href` loads that page, a click on a textbox focuses it,
`Type` sets or extends the target's or focused textbox's value, and `snapshot()` numbers the elements as refs. It has
no layout, so a `Point` hits nothing; hooks see the action and can test the coordinates themselves.
"""

from __future__ import annotations

import copy
import struct
import zlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from functools import cache
from urllib.parse import urljoin, urlsplit

from sammy.browser.contract import (
    Action,
    Click,
    ElementTarget,
    Feature,
    LifecycleError,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    NotSupported,
    Point,
    Press,
    Ref,
    Screenshot,
    Scroll,
    Selector,
    Snapshot,
    TargetNotFound,
    Type,
    features_of,
)
from sammy.browser.live import Outline, OutlineItem
from sammy.browser.state import BLANK_URL, BrowserState, Cookie, origin_of

ENGINE = 'fake'
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

OnLoad = Callable[['FakeBrowser'], None]
OnAction = Callable[['FakeBrowser', Action], None]


@dataclass(kw_only=True)
class FakeElement:
    selector: str
    """What a `Selector` must equal to find this element, such as `#add-eggs`. The fake does not parse CSS."""
    role: str
    """`button`, `link`, `textbox` and so on. A click on a `textbox` focuses it."""
    name: str
    href: str | None = None
    """Clicking loads this URL, resolved against the page's."""
    value: str = ''
    """A textbox's value."""
    secret: bool = False
    """A password field: the snapshot masks its value."""


@dataclass(kw_only=True)
class FakePage:
    """A page template. Each load works on a fresh copy, so values typed on one visit are gone on the next."""

    title: str = ''
    text: str = ''
    """The page's visible text, before its elements."""
    elements: list[FakeElement] = field(default_factory=list[FakeElement])
    on_load: OnLoad | None = None
    """Runs when the page loads, as the server and the page's scripts would. It may change `browser.page`, the
    cookies and the storage."""
    on_action: OnAction | None = None
    """Runs for every action on this page except `Navigate`, after the fake's own handling of focus and typing and
    before a link's navigation."""


_BLANK_PAGE = FakePage()
_NOT_FOUND_PAGE = FakePage(title='Not found', text='Not found')


class FakeBrowser:
    """An in-memory `BrowserBackend`.

    `pages` maps URLs (without the fragment) to page templates; any other URL loads a "Not found" page, as a real
    browser shows a server's 404. Features in `not_supported` raise `NotSupported`, to stand in for a weaker engine.

    Hooks and tests may read and change `page`, `cookies`, `local_storage` and `session_storage` directly. `actions`
    records every action performed, across opens and closes.
    """

    def __init__(
        self,
        *,
        pages: Mapping[str, FakePage] | None = None,
        not_supported: Collection[Feature] = (),
        engine: str = ENGINE,
    ) -> None:
        self.pages: dict[str, FakePage] = dict(pages or {})
        self.not_supported = frozenset(not_supported)
        self.engine = engine
        self.actions: list[Action] = []
        self._open = False
        self._reset()

    def _reset(self) -> None:
        self.url = BLANK_URL
        self.page = copy.deepcopy(_BLANK_PAGE)
        self.cookies: list[Cookie] = []
        self.local_storage: dict[str, dict[str, str]] = {}
        self.session_storage: dict[str, dict[str, str]] = {}
        """Origin to items for the one tab, kept across navigations as a real tab keeps it."""
        self.focused: FakeElement | None = None
        self._refs: list[FakeElement] | None = None

    # --- for hooks and tests ---

    @property
    def is_open(self) -> bool:
        return self._open

    def load(self, url: str) -> None:
        """Load `url` (resolved against the current URL) in the tab and run its `on_load`."""
        url = urljoin(self.url, url).partition('#')[0]
        template = self.pages.get(url) or (_BLANK_PAGE if url == BLANK_URL else _NOT_FOUND_PAGE)
        self.url = url
        self.page = copy.deepcopy(template)
        self.focused = None
        self._refs = None
        if self.page.on_load is not None:
            self.page.on_load(self)

    def find(self, css: str) -> FakeElement | None:
        """The current page's element whose `selector` equals `css`."""
        return next((e for e in self.page.elements if e.selector == css), None)

    def resolve(self, target: ElementTarget) -> FakeElement:
        """The element `target` points at, as `act` finds it. Raises `TargetNotFound`."""
        match target:
            case Selector(css=css):
                element = self.find(css)
                if element is None:
                    raise TargetNotFound(target)
                return element
            case Ref(ref=ref):
                if self._refs is None:
                    raise TargetNotFound(target, 'no snapshot of this page yet; refs reset when a page loads')
                if not ref.isdigit() or not 1 <= int(ref) <= len(self._refs):
                    raise TargetNotFound(target, 'not a ref in the latest snapshot')
                return self._refs[int(ref) - 1]

    def cookies_for(self, url: str) -> list[Cookie]:
        """The cookies a request to `url` carries, as a server would see them: matched by domain, path and
        `secure`."""
        parts = urlsplit(url)
        host = parts.hostname or ''
        path = parts.path or '/'

        def matches(cookie: Cookie) -> bool:
            if cookie.domain.startswith('.'):
                domain = cookie.domain[1:]
                if host != domain and not host.endswith(cookie.domain):
                    return False
            elif host != cookie.domain:
                return False
            if cookie.secure and parts.scheme != 'https':
                return False
            return path == cookie.path or path.startswith(cookie.path.rstrip('/') + '/')

        return [c for c in self.cookies if matches(c)]

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._open:
            raise LifecycleError('the browser is already open')
        self._reset()
        self._open = True
        if state is not None:
            self.cookies = list(state.cookies)
            self.local_storage = {origin: dict(items) for origin, items in state.local_storage.items()}
            self.session_storage = {origin: dict(items) for origin, items in state.session_storage.items()}
            self.load(state.url)

    async def export(self) -> BrowserState:
        self._check_open()
        self._check_supported('export')
        origin = origin_of(self.url)
        session = self.session_storage.get(origin, {}) if origin else {}
        return BrowserState(
            url=self.url,
            cookies=list(self.cookies),
            local_storage={o: dict(items) for o, items in self.local_storage.items() if items},
            session_storage={origin: dict(session)} if origin and session else {},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._check_open()
        self._check_supported('snapshot')
        self._refs = list(self.page.elements)
        lines = [self.page.text] if self.page.text else []
        for number, element in enumerate(self._refs, start=1):
            line = f'[{number}] {element.role} "{element.name}"'
            if element.value:
                line += f' value="{"***" if element.secret else element.value}"'
            lines.append(line)
        return Snapshot(url=self.url, title=self.page.title, text='\n'.join(lines))

    async def act(self, action: Action) -> None:
        self._check_open()
        for feature in features_of(action):
            self._check_supported(feature)
        page = self.page
        href: str | None = None
        match action:
            case Navigate():
                self.actions.append(action)
                self.load(action.url)
                return
            case Click():
                if not isinstance(action.target, Point):
                    element = self.resolve(action.target)
                    if element.role == 'textbox':
                        self.focused = element
                    href = element.href
            case Type():
                if action.target is not None:
                    self.focused = self.resolve(action.target)
                    self.focused.value = action.text
                elif self.focused is not None:
                    self.focused.value += action.text
            case Press() | Scroll() | MouseDown() | MouseMove() | MouseUp():
                pass
        self.actions.append(action)
        if page.on_action is not None:
            page.on_action(self, action)
        if href is not None and self.page is page:
            self.load(href)

    async def outline(self) -> Outline:
        """`OutlineBackend`: the page's text lines, then its elements, with which has the focus. A password's value
        is only its length, as `outline.js` gives it. The fake has no layout, so every box is empty."""
        self._check_open()
        lines = [
            OutlineItem(role='text', name=line, x=0, y=0, width=0, height=0) for line in self.page.text.splitlines()
        ]
        elements = [
            OutlineItem(
                role=element.role,
                name=element.name,
                x=0,
                y=0,
                width=0,
                height=0,
                value=f'{len(element.value)} characters' if element.secret and element.value else element.value,
                focused=element is self.focused,
                secure=element.secret,
            )
            for element in self.page.elements
        ]
        return Outline(title=self.page.title, items=(*lines, *elements))

    async def screenshot(self) -> Screenshot:
        self._check_open()
        self._check_supported('screenshot')
        return Screenshot(png=_blank_png(), width=VIEWPORT_WIDTH, height=VIEWPORT_HEIGHT)

    async def close(self) -> None:
        self._open = False
        self._reset()

    # --- internals ---

    def _check_open(self) -> None:
        if not self._open:
            raise LifecycleError('the browser is not open')

    def _check_supported(self, feature: Feature) -> None:
        if feature in self.not_supported:
            raise NotSupported(feature, engine=self.engine)


@cache
def _blank_png() -> bytes:
    """A white PNG the size of the viewport."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    header = struct.pack('>IIBBBBB', VIEWPORT_WIDTH, VIEWPORT_HEIGHT, 8, 0, 0, 0, 0)  # 8-bit greyscale
    rows = (b'\x00' + b'\xff' * VIEWPORT_WIDTH) * VIEWPORT_HEIGHT
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')
