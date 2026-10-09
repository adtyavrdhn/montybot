"""Reading and searching the web without the browser (`sammy.web`): public addresses only, redirects included, a cap
on what is downloaded and a timeout; pages as text; search through Tavily's API. All offline, against `FakeWeb`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from sites.web import SEARCH_KEY, FakeWeb

from sammy import web
from sammy.integrations import egress

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def site() -> Iterator[FakeWeb]:
    site = FakeWeb()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def public(site: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> str:
    """The fake site as if it were on the internet: `public.test` is its name, and its address counts as public.
    Every other name and address is checked as it is in the app."""
    real = egress.public_address

    async def resolve(host: str, port: int, *, allow_private: bool) -> str:
        return '127.0.0.1' if host == 'public.test' else await real(host, port, allow_private=allow_private)

    monkeypatch.setattr(egress, 'public_address', resolve)
    return f'http://public.test:{site.port}'


def private_urls(site: FakeWeb) -> list[str]:
    return [
        f'http://127.0.0.1:{site.port}/secret',
        f'http://localhost:{site.port}/secret',
        f'http://[::1]:{site.port}/secret',
        'http://169.254.169.254/latest/meta-data/',
        'http://10.0.0.1/',
    ]


async def test_a_page_is_read_as_text(public: str) -> None:
    page = await web.fetch(f'{public}/pharmacy', allow_private=False)
    assert page.url == f'{public}/pharmacy'
    assert page.title == 'Corner Pharmacy'
    assert '# Corner Pharmacy' in page.text
    assert 'Today | Closes at 6pm' in page.text
    assert f'[Prescriptions]({public}/prescriptions)' in page.text
    assert 'tracking' not in page.text  # scripts are not text
    assert page.shown(20_000).startswith(f'URL: {public}/pharmacy\nTitle: Corner Pharmacy\n\n')


async def test_private_addresses_are_refused(site: FakeWeb) -> None:
    for url in private_urls(site):
        with pytest.raises(web.WebError, match='private network'):
            await web.fetch(url, allow_private=False)
    assert site.paths == []


async def test_a_redirect_to_a_private_address_is_refused(site: FakeWeb, public: str) -> None:
    for url in private_urls(site):
        with pytest.raises(web.WebError, match='private network'):
            await web.fetch(f'{public}/redirect?to={quote(url)}', allow_private=False)
    assert '/secret' not in site.paths
    assert site.paths == ['/redirect'] * len(private_urls(site))


async def test_redirects_are_followed_a_few_times(site: FakeWeb, public: str) -> None:
    page = await web.fetch(f'{public}/redirect?to=/pharmacy', allow_private=False)
    assert page.url == f'{public}/pharmacy'
    with pytest.raises(web.WebError, match=f'redirected more than {web.MAX_REDIRECTS} times'):
        await web.fetch(f'{public}/loop', allow_private=False)
    assert site.paths.count('/loop') == web.MAX_REDIRECTS + 1
    with pytest.raises(web.WebError, match='only http and https'):
        await web.fetch(f'{public}/redirect?to={quote("file:///etc/passwd")}', allow_private=False)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://public.test/x', 'javascript:alert(1)', 'http:///x'])
async def test_only_web_addresses_are_read(url: str) -> None:
    with pytest.raises(web.WebError, match='only http and https'):
        await web.fetch(url, allow_private=True)


async def test_what_is_downloaded_is_capped(site: FakeWeb, public: str) -> None:
    page = await web.fetch(f'{public}/endless', allow_private=False)
    assert page.cut and len(page.text) == web.MAX_BYTES
    await asyncio.sleep(0.5)
    # The reader stopped at the cap: the server sent a little more (socket buffers), not the 125 MB it had.
    assert site.streamed < 16 * web.MAX_BYTES
    assert page.shown(1000).endswith(f'[cut at 1000 of {web.MAX_BYTES} characters]')
    assert page.shown(10**9).endswith(f'[cut at {web.MAX_CHARS} of {web.MAX_BYTES} characters]')
    whole = web.Page(url='https://x.example/', title='', text='short', cut=True)
    assert whole.shown(1000).endswith('[cut: the page is over 2 MB]')


async def test_a_slow_site_times_out(public: str) -> None:
    with pytest.raises(web.WebError, match='did not answer within 0.5 seconds'):
        await web.fetch(f'{public}/slow', timeout=0.5, allow_private=False)


async def test_errors_and_files_are_explained(public: str) -> None:
    with pytest.raises(web.WebError, match='answered 404 Not Found'):
        await web.fetch(f'{public}/missing', allow_private=False)
    with pytest.raises(web.WebError, match='is a application/pdf file, not a page'):
        await web.fetch(f'{public}/leaflet.pdf', allow_private=False)


def test_html_becomes_text() -> None:
    html = """<html><head><title> A   page </title><style>p { color: red }</style></head><body>
    <a href="#main">Skip to content</a> <a href="javascript:go()">Menu</a>
    <h2>Opening   hours</h2><p>Open
    every day.</p><noscript>Turn on JavaScript</noscript>
    <ul><li>Mon</li><li><a href="https://other.example/x?y=1">Elsewhere</a></li></ul></body></html>"""
    title, text = web.page_text(html, 'https://shop.example/hours')
    assert title == 'A page'
    assert text == (
        'Skip to content Menu\n## Opening hours\nOpen every day.\n- Mon\n- [Elsewhere](https://other.example/x?y=1)'
    )


async def test_search(site: FakeWeb) -> None:
    found = await web.search('pharmacy hours', max_results=3, api_key=SEARCH_KEY, url=f'{site.url}/search')
    assert [result.url for result in found] == [f'{site.url}/pharmacy'] * 3
    assert site.searches == [{'query': 'pharmacy hours', 'max_results': 3}]
    await web.search('pharmacy hours', max_results=50, api_key=SEARCH_KEY, url=f'{site.url}/search')
    assert site.searches[-1]['max_results'] == web.MAX_RESULTS
    assert web.shown_results('q', found[:1]) == f'1. Corner Pharmacy\n{site.url}/pharmacy\nOpening hours'
    assert web.shown_results('q', []) == "Nothing found for 'q'."


async def test_a_failed_search_says_so(site: FakeWeb) -> None:
    with pytest.raises(web.WebError, match='answered 401'):
        await web.search('x', max_results=3, api_key='wrong', url=f'{site.url}/search')
    with pytest.raises(web.WebError, match='could not be reached'):
        await web.search('x', max_results=3, api_key=SEARCH_KEY, url='http://127.0.0.1:9/search')
