"""Reading and searching the web without the browser, for `web_fetch` and `web_search` (`sammy.web_tools`) on a model
that has no web tools of its own.

A page is fetched with `egress.public_client`: every connection, each redirect's included, goes to a public address
only, checked as it opens, and is not traced (a URL can hold a key). Redirects are followed here, at most
`MAX_REDIRECTS`, each through the same check. The body is read up to `MAX_BYTES`, and the whole fetch, redirects
included, has `FETCH_TIMEOUT_SECONDS`. HTML becomes text with its headings, lists and links (`page_text`); other text
comes as it is; anything else is refused with words for the model.

Search is Tavily's search API (`TAVILY_API_KEY`), whose results are short extracts made for models.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import urljoin, urlsplit

import httpx
import httpx2
from pydantic import TypeAdapter, ValidationError

from sammy.integrations import egress

MAX_BYTES = 2 * 1024 * 1024
"""The most of a response body that is read; the rest is never downloaded."""
MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 30.0
MAX_CHARS = 100_000
"""The most text `web_fetch` returns, whatever the model asks for."""
MAX_RESULTS = 10
HEADERS = {
    # Some sites send Markdown when asked, which is shorter than their HTML.
    'Accept': 'text/markdown, text/html;q=0.9, text/plain;q=0.8, */*;q=0.5',
    'User-Agent': 'Mozilla/5.0 (compatible; Sammy)',
}
HTML_TYPES = ('text/html', 'application/xhtml+xml')
TEXT_TYPES = ('application/json', 'application/xml', 'application/javascript', 'application/ld+json')


class WebError(Exception):
    """Why a page could not be read or a search failed, in words for the model."""


@dataclass(frozen=True)
class Page:
    url: str
    """Where the page was in the end, after redirects."""
    title: str
    text: str
    cut: bool = False
    """The body was over `MAX_BYTES`, so the text is only its start."""

    def shown(self, max_chars: int) -> str:
        """The page for the model, as `read_page` shows one: `URL:`, `Title:`, then the text, cut at `max_chars`."""
        max_chars = min(max(max_chars, 1), MAX_CHARS)
        text = self.text
        if len(text) > max_chars:
            text = f'{text[:max_chars]}\n[cut at {max_chars} of {len(self.text)} characters]'
        elif self.cut:
            text += f'\n[cut: the page is over {MAX_BYTES >> 20} MB]'
        return f'URL: {self.url}\nTitle: {self.title}\n\n{text}'


def check(url: str) -> str:
    """The URL if it may be fetched: http(s) with a server name. Private addresses are refused as they connect."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise WebError('only http and https addresses can be read.')
    return url.strip()


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


async def fetch(url: str, *, allow_private: bool, timeout: float = FETCH_TIMEOUT_SECONDS) -> Page:
    """The page at `url`, following redirects. Raises `WebError`."""
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(check(url), allow_private=allow_private)
    except TimeoutError:
        raise WebError(f'{host_of(url)} did not answer within {timeout:g} seconds.') from None


async def _fetch(url: str, *, allow_private: bool) -> Page:
    async with egress.public_client(allow_private=allow_private, headers=HEADERS) as http:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with http.stream('GET', url) as response:
                    if response.is_redirect:
                        url = check(urljoin(url, response.headers.get('location', '')))
                        continue
                    if response.status_code >= 400:
                        raise WebError(f'{host_of(url)} answered {response.status_code} {response.reason_phrase}.')
                    body, cut = await read_capped(response)
                    content_type = response.headers.get('content-type', '')
                    encoding = response.charset_encoding
            except (httpx2.HTTPError, httpx2.InvalidURL) as error:  # a refused address is a `ConnectError`
                raise WebError(f'{host_of(url)} could not be read: {error}') from None
            return await asyncio.to_thread(page_of, url, content_type, body, encoding, cut)
    raise WebError(f'{host_of(url)} redirected more than {MAX_REDIRECTS} times.')


async def read_capped(response: httpx2.Response) -> tuple[bytes, bool]:
    """The body, up to `MAX_BYTES`, and whether there was more. The rest is never read."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        if size + len(chunk) > MAX_BYTES:
            chunks.append(chunk[: MAX_BYTES - size])
            return b''.join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks), False


def page_of(url: str, content_type: str, body: bytes, encoding: str | None, cut: bool) -> Page:
    media_type = content_type.split(';')[0].strip().lower()
    try:
        text = body.decode(encoding or 'utf-8', errors='replace')
    except LookupError:  # a charset Python does not know
        text = body.decode('utf-8', errors='replace')
    if media_type in HTML_TYPES or (not media_type and text.lstrip()[:1] == '<'):
        title, text = page_text(text, url)
        return Page(url=url, title=title, text=text, cut=cut)
    if (
        not media_type
        or media_type.startswith('text/')
        or media_type in TEXT_TYPES
        or media_type.endswith(('+json', '+xml'))
    ):
        return Page(url=url, title='', text=text.strip(), cut=cut)
    raise WebError(f'{url} is a {media_type} file, not a page. Open it in your browser to download it.')


SKIPPED = frozenset({'script', 'style', 'noscript', 'template', 'svg', 'iframe', 'object', 'canvas'})
BLOCKS = frozenset(
    {
        'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'details', 'div', 'dl', 'dt', 'fieldset',
        'figcaption', 'figure', 'footer', 'form', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section',
        'summary', 'table', 'tr', 'ul', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    }
)  # fmt: skip
SPACES = re.compile(r'\s+')


class _Text(HTMLParser):
    """HTML to text: headings as `#`, list items as `-`, table cells split by `|`, and links as `[text](url)`."""

    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.parts: list[str] = []
        self.title = ''
        self._in_title = False
        self._skipping = 0
        self._link: tuple[int, str] | None = None
        """Where the open link's text starts in `parts`, and its address."""

    def _line(self) -> None:
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def _add(self, text: str) -> None:
        if not self.parts or self.parts[-1].endswith(('\n', ' ')):
            text = text.lstrip()
        if text:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED:
            self._skipping += 1
        if tag == 'title':
            self._in_title = True
        if self._skipping:
            return
        if tag in BLOCKS:
            self._line()
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._add('#' * int(tag[1]) + ' ')
        elif tag == 'li':
            self._add('- ')
        elif tag in ('td', 'th') and self.parts and not self.parts[-1].endswith('\n'):
            self._add(' | ')
        elif tag == 'a':
            href = urljoin(self.base, dict(attrs).get('href') or '')
            # Not a link to a place on this page (`#main`): nothing to fetch.
            if urlsplit(href).scheme in ('http', 'https') and href.split('#')[0] != self.base.split('#')[0]:
                self._link = (len(self.parts), href)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED:
            self._skipping = max(self._skipping - 1, 0)
        elif tag == 'title':
            self._in_title = False
        elif tag == 'a' and self._link is not None:
            start, href = self._link
            self._link = None
            text = ''.join(self.parts[start:]).strip()
            if text:
                self.parts[start:] = [f'[{text}]({href})']
        elif tag in BLOCKS:
            self._line()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skipping:
            self._add(SPACES.sub(' ', data))


def page_text(html: str, url: str) -> tuple[str, str]:
    """The page's title and its text."""
    parser = _Text(url)
    parser.feed(html)
    parser.close()
    lines = (line.strip() for line in ''.join(parser.parts).splitlines())
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    return SPACES.sub(' ', parser.title).strip(), text


class _Result(TypedDict):
    title: str
    url: str
    content: str


class _Results(TypedDict):
    results: list[_Result]


_results = TypeAdapter(_Results)


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    extract: str


async def search(query: str, *, max_results: int, api_key: str, url: str) -> list[Result]:
    """Tavily's results for `query`, at most `max_results` (1 to `MAX_RESULTS`). Raises `WebError`."""
    max_results = min(max(max_results, 1), MAX_RESULTS)
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as http:
            response = await http.post(
                url,
                json={'query': query, 'max_results': max_results},
                headers={'Authorization': f'Bearer {api_key}'},
            )
    except httpx.HTTPError:
        raise WebError('The search service could not be reached. Search in your browser instead.') from None
    if response.status_code != 200:
        raise WebError(f'The search service answered {response.status_code}. Search in your browser instead.')
    try:
        found = _results.validate_json(response.content)['results']
    except ValidationError:
        raise WebError('The search service gave an answer that could not be read.') from None
    return [Result(title=r['title'], url=r['url'], extract=r['content']) for r in found[:max_results]]


def shown_results(query: str, results: list[Result]) -> str:
    if not results:
        return f'Nothing found for {query!r}.'
    return '\n\n'.join(f'{n}. {r.title}\n{r.url}\n{r.extract.strip()}' for n, r in enumerate(results, 1))
