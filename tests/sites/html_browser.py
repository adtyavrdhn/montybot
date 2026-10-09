"""`HtmlBrowser`: the fake engine for the fixture sites. A `FakeBrowser` whose pages come from real HTTP.

It fetches each page from the site, keeps cookies the way a browser does (HttpOnly included, sent back by domain and
path), and turns the HTML into a `FakePage`: the visible text, and an element for every link, button and text box.
Forms submit, links follow, redirects carry their `Set-Cookie`. It runs no JavaScript, so the fixture sites render
everything on the server, as most real shops do; the press-and-hold check is the one scripted widget, and it is
emulated here the way the page's script does it.

Select it with `SAMMY_BROWSER=fake` in the end-to-end tests (the default). `chromium` drives the same sites in a
real browser instead.

It is a test double, not a browser: no CSS, no layout (a mouse point hits the page's press-and-hold button if it has
one, and nothing else), no scripts.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urljoin, urlsplit

from sammy.browser.contract import Action, Click, Download, MouseDown, MouseUp, Press, Ref, Selector
from sammy.browser.fake import VIEWPORT_HEIGHT, VIEWPORT_WIDTH, FakeBrowser, FakeElement, FakePage
from sammy.browser.state import BLANK_URL, BrowserState, Cookie

ENGINE = 'fake (html)'
BLOCKS = {'p', 'div', 'h1', 'h2', 'h3', 'li', 'tr', 'table', 'ul', 'ol', 'form', 'section', 'br'}


@dataclass(kw_only=True)
class HtmlElement(FakeElement):
    form: int | None = None
    field_name: str | None = None
    submits: bool = False
    post: str | None = None
    """`data-post`: clicking POSTs to this path."""
    hold_ms: int | None = None
    """`data-hold-ms`: a press-and-hold button that POSTs `post` once held this long."""
    box: tuple[float, float, float, float] | None = None
    """Where a fixed-position element sits, as fractions of the viewport (left, top, right, bottom), from its
    `style`. The only layout this engine knows."""
    autofocus: bool = False


@dataclass
class Form:
    method: str
    action: str
    hidden: dict[str, str]


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.text: list[str] = []
        self.elements: list[HtmlElement] = []
        self.forms: list[Form] = []
        self._form: int | None = None
        self._skip = 0
        self._in_title = False
        self._open: HtmlElement | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or '' for k, v in attrs}
        if tag in ('script', 'style'):
            self._skip += 1
        elif tag == 'title':
            self._in_title = True
        elif tag in BLOCKS:
            self.text.append('\n')
        elif tag in ('td', 'th'):
            self.text.append(' | ')
        if tag == 'form':
            self.forms.append(Form(a.get('method', 'get').lower(), a.get('action', ''), {}))
            self._form = len(self.forms) - 1
        elif tag == 'input':
            kind = a.get('type', 'text')
            if kind == 'hidden':
                if self._form is not None:
                    self.forms[self._form].hidden[a.get('name', '')] = a.get('value', '')
                return
            label = a.get('aria-label') or (a.get('value') if kind == 'submit' else '') or a.get('placeholder')
            label = label or a.get('name') or kind
            self.elements.append(
                HtmlElement(
                    selector=_selector(a, len(self.elements)),
                    role='button' if kind == 'submit' else 'textbox',
                    name=label,
                    value=a.get('value', '') if kind != 'submit' else '',
                    secret=kind == 'password',
                    form=self._form,
                    field_name=a.get('name'),
                    submits=kind == 'submit',
                    autofocus='autofocus' in a,
                )
            )
        elif tag in ('a', 'button'):
            hold = a.get('data-hold-ms')
            self._open = HtmlElement(
                selector=_selector(a, len(self.elements)),
                role='link' if tag == 'a' else 'button',
                name=a.get('aria-label', ''),
                href=a.get('href') if tag == 'a' else None,
                form=self._form if tag == 'button' else None,
                submits=tag == 'button' and a.get('type', 'submit') == 'submit' and self._form is not None,
                post=a.get('data-post') or None,
                hold_ms=int(hold) if hold else None,
                box=_box(a.get('style', '')),
            )
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ('script', 'style'):
            self._skip -= 1
        elif tag == 'title':
            self._in_title = False
        elif tag == 'form':
            self._form = None
        elif tag in ('a', 'button') and self._open is not None:
            if not self._open.name:
                self._open.name = ' '.join(''.join(self._label).split())
            self.elements.append(self._open)
            self._open = None
        if tag in BLOCKS:
            self.text.append('\n')

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.text.append(data)
        if self._open is not None:
            self._label.append(data)


def _box(style: str) -> tuple[float, float, float, float] | None:
    """`left`, `top`, `width` and `height` in percent from a `position:fixed` style, else None."""
    rules = dict(rule.split(':', 1) for rule in style.replace(' ', '').split(';') if ':' in rule)
    if rules.get('position') != 'fixed':
        return None
    try:
        left, top, width, height = (float(rules[k].removesuffix('%')) / 100 for k in ('left', 'top', 'width', 'height'))
    except (KeyError, ValueError):
        return None
    return left, top, left + width, top + height


def _selector(attrs: dict[str, str], index: int) -> str:
    if attrs.get('id'):
        return f'#{attrs["id"]}'
    if attrs.get('name'):
        return f'[name="{attrs["name"]}"]'
    return f'@{index}'


def _visible_text(chunks: list[str]) -> str:
    lines = [' '.join(line.split()) for line in ''.join(chunks).split('\n')]
    return '\n'.join(line for line in lines if line)


def _expiry(max_age: str, expires: str) -> float:
    """Seconds since the epoch, or -1 for a session cookie. `Max-Age` wins over `Expires`; zero or less is past."""
    if max_age:
        try:
            return time.time() + int(max_age) if int(max_age) > 0 else 0
        except ValueError:
            pass
    if expires:
        try:
            return parsedate_to_datetime(expires).timestamp()
        except (TypeError, ValueError):
            pass
    return -1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class HtmlBrowser(FakeBrowser):
    def __init__(self) -> None:
        self.downloads: list[Download] = []
        """What links and forms downloaded instead of showing, in order."""
        self._untaken = 0
        super().__init__(engine=ENGINE)

    async def take_downloads(self) -> list[Download]:
        """`DownloadsBackend`: the downloads since the last call, which the service saves in the run's files."""
        taken, self._untaken = self.downloads[len(self.downloads) - self._untaken :], 0
        return taken

    def _reset(self) -> None:
        super()._reset()
        self.forms: list[Form] = []
        self._held_since: float | None = None

    # --- loading ---

    def load(self, url: str) -> None:
        url = urljoin(self.url, url).partition('#')[0]
        if url == BLANK_URL or not url.startswith('http'):
            super().load(url)
            return
        self._request('GET', url, None)

    def _request(self, method: str, url: str, form: dict[str, str] | None) -> None:
        for _ in range(10):
            data = urlencode(form).encode() if form is not None else None
            request = urllib.request.Request(url, data=data, method=method)
            if data is not None:
                request.add_header('Content-Type', 'application/x-www-form-urlencoded')
            cookie_header = '; '.join(f'{c.name}={c.value}' for c in self.cookies_for(url))
            if cookie_header:
                request.add_header('Cookie', cookie_header)
            try:
                response = _opener.open(request, timeout=10)
                status, headers, body = response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                status, headers, body = error.code, error.headers, error.read()
            for value in headers.get_all('Set-Cookie') or []:
                self._set_cookie(url, value)
            if status in (301, 302, 303, 307, 308) and headers.get('Location'):
                url = urljoin(url, headers['Location'])
                if status in (301, 302, 303):  # 307 and 308 repeat the request as it was
                    method, form = 'GET', None
                continue
            kind = headers.get('Content-Type', '').split(';')[0].strip()
            if 'attachment' in headers.get('Content-Disposition', '') or kind not in ('text/html', ''):
                # A browser saves it, under the name the site suggests, and stays on the page.
                self.downloads.append(Download(name=_file_name(url, headers.get('Content-Disposition', '')), data=body))
                self._untaken += 1
                return
            self._show(url, body.decode(errors='replace'))
            return
        raise RuntimeError('too many redirects')

    def _show(self, url: str, html: str) -> None:
        parser = _Parser()
        parser.feed(html)
        self.forms = parser.forms
        self.url = url
        self.page = FakePage(
            title=' '.join(parser.title.split()),
            text=_visible_text(parser.text),
            elements=list(parser.elements),
            on_action=self._on_action,
        )
        self.focused = next((e for e in parser.elements if e.autofocus), None)
        self._refs = None

    def _set_cookie(self, url: str, header: str) -> None:
        parsed = SimpleCookie()
        parsed.load(header)
        host = urlsplit(url).hostname or ''
        for name, morsel in parsed.items():
            expires = _expiry(morsel['max-age'], morsel['expires'])
            domain = morsel['domain'].lstrip('.')
            if domain and host != domain and not host.endswith('.' + domain):
                continue  # a site may not set cookies for another
            cookie = Cookie(
                name=name,
                value=morsel.value,
                domain=f'.{domain}' if domain else host,
                path=morsel['path'] or '/',
                expires=expires,
                http_only=bool(morsel['httponly']),
                secure=bool(morsel['secure']),
                same_site=(morsel['samesite'] or 'Lax').capitalize(),  # pyright: ignore[reportArgumentType]
            )
            self.cookies = [
                c for c in self.cookies if (c.name, c.domain, c.path) != (cookie.name, cookie.domain, cookie.path)
            ]
            if expires == -1 or expires > time.time():
                self.cookies.append(cookie)

    def cookies_for(self, url: str) -> list[Cookie]:
        now = time.time()
        return [c for c in super().cookies_for(url) if c.expires == -1 or c.expires > now]

    # --- what the page's forms and widgets do ---

    def _on_action(self, browser: FakeBrowser, action: Action) -> None:
        match action:
            case Click(target=Selector() | Ref() as target):
                element = self.resolve(target)
                if isinstance(element, HtmlElement):
                    self._activate(element)
            case Press(key='Enter'):
                focused = self.focused
                if not isinstance(focused, HtmlElement):
                    return
                if focused.role == 'link' and focused.href:
                    self.load(focused.href)
                elif focused.role == 'button':
                    self._activate(focused)
                elif focused.form is not None:
                    self._submit(focused.form)  # Enter in a text box submits its form
            case Press(key='Tab'):
                self._tab('Shift' in action.modifiers)
            case MouseDown(at=at):
                if self._hold_button(at.x, at.y) is not None:
                    self._held_since = time.monotonic()
            case MouseUp(at=at):
                button, since, self._held_since = self._hold_button(at.x, at.y), self._held_since, None
                if button is not None and since is not None and self._held_long_enough(button, since):
                    self._request('POST', urljoin(self.url, button.post or ''), {})
            case _:
                pass

    def _activate(self, element: HtmlElement) -> None:
        if element.hold_ms is not None:
            return  # a click is not a hold
        if element.post:
            self._request('POST', urljoin(self.url, element.post), {})
        elif element.submits and element.form is not None:
            self._submit(element.form)
        elif element.role == 'link' and element.href:
            pass  # FakeBrowser follows `href` after this hook

    def _submit(self, index: int) -> None:
        form = self.forms[index]
        values = dict(form.hidden)
        for element in self.page.elements:
            if not isinstance(element, HtmlElement) or element.form != index:
                continue
            if element.role == 'textbox' and element.field_name:
                values[element.field_name] = element.value
        target = urljoin(self.url, form.action or self.url)
        if form.method == 'post':
            self._request('POST', target, values)
        else:
            self._request('GET', urlsplit(target)._replace(query=urlencode(values), fragment='').geturl(), None)

    def _tab(self, backwards: bool) -> None:
        focusable = [e for e in self.page.elements if e.role in ('textbox', 'button', 'link')]
        if not focusable:
            return
        index = focusable.index(self.focused) if self.focused in focusable else -1
        index = (index - 1) if backwards else (index + 1)
        self.focused = focusable[index % len(focusable)]

    @staticmethod
    def _held_long_enough(button: HtmlElement, since: float) -> bool:
        return bool(button.post) and button.hold_ms is not None and (time.monotonic() - since) * 1000 >= button.hold_ms

    def _hold_button(self, x: float, y: float) -> HtmlElement | None:
        """The press-and-hold button under the point, if there is one there."""
        fx, fy = x / VIEWPORT_WIDTH, y / VIEWPORT_HEIGHT
        for element in self.page.elements:
            if isinstance(element, HtmlElement) and element.hold_ms is not None and element.box is not None:
                left, top, right, bottom = element.box
                if left <= fx <= right and top <= fy <= bottom:
                    return element
        return None


def _file_name(url: str, disposition: str) -> str:
    """The `filename` of a `Content-Disposition` header, else the last part of the URL's path."""
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return match.group(1) if match else urlsplit(url).path.rsplit('/', 1)[-1]


def new_backend() -> HtmlBrowser:
    return HtmlBrowser()


@dataclass
class _Jar:
    cookies: list[Cookie] = field(default_factory=list[Cookie])


class TabbedHtmlBrowser(HtmlBrowser):
    """`HtmlBrowser` with tabs (`TabsBackend`), as Chrome has, for subagents (#132): the runs of one user share its
    cookies, each in a tab of its own. Pages load on a thread, so tabs load side by side as a real browser's do. The
    tabs' cookies are one jar, read before and written back after each load."""

    def __init__(self, jar: _Jar | None = None) -> None:
        self._jar = jar if jar is not None else _Jar()
        self._is_tab = jar is not None
        super().__init__()

    def new_tab(self) -> TabbedHtmlBrowser:
        return TabbedHtmlBrowser(self._jar)

    async def open(self, state: BrowserState | None) -> None:
        if self._is_tab:  # a tab has the browser's own cookies, and only goes to the state's page
            state = BrowserState(url=state.url if state is not None else BLANK_URL, cookies=self._jar.cookies)
        await self._off_loop(lambda: HtmlBrowser.open(self, state))

    async def act(self, action: Action) -> None:
        await self._off_loop(lambda: HtmlBrowser.act(self, action))

    async def export(self) -> BrowserState:
        self.cookies = list(self._jar.cookies)
        return await super().export()

    async def _off_loop(self, work: Callable[[], Coroutine[object, object, None]]) -> None:
        self.cookies = list(self._jar.cookies)
        await asyncio.to_thread(asyncio.run, work())
        self._jar.cookies = list(self.cookies)


def new_tabbed_backend() -> TabbedHtmlBrowser:
    return TabbedHtmlBrowser()
