"""A lookup is answered without the browser (#129): the agent searches with `web_search`, reads the result with
`web_fetch`, and the run's trace has no browser in it. A model's own web tools come first; Sammy's are for a model
without them, or with `NATIVE_WEB_TOOLS=false`, and `web_search` needs a Tavily key."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Client
from helpers import eventually
from pydantic import SecretStr
from sites.web import SEARCH_KEY, FakeWeb
from test_traces import serve_traced

pytestmark = pytest.mark.scripted


def test_a_lookup_opens_no_browser(database_url: str, workspaces_dir: Path) -> None:
    site = FakeWeb()
    site.start()
    try:
        with serve_traced(
            database_url,
            workspaces_dir,
            logfire_include_content=False,
            tavily_api_key=SecretStr(SEARCH_KEY),
            tavily_url=f'{site.url}/search',
        ) as (app, exporter):
            client = Client(app)  # pyright: ignore[reportArgumentType]
            client.sign_up()
            reply = client.wait_for_reply(client.ask('When does the pharmacy close today?'))
            eventually(
                lambda: any(span.name == 'run.lifecycle' for span in exporter.get_finished_spans()) or None,
                what='the run to finish',
            )
            spans = exporter.get_finished_spans()
    finally:
        site.stop()

    assert reply == 'Today | Closes at 6pm'
    assert site.searches == [{'query': 'Corner Pharmacy opening hours', 'max_results': 3}]
    assert site.paths == ['/search', '/pharmacy']
    names = {span.name for span in spans}
    assert {'execute_tool web_search', 'execute_tool web_fetch', 'web.search', 'web.fetch'} <= names, sorted(names)
    # Every run ends by closing its browser, if it had one; nothing else of the browser may be there.
    browser = [name for name in names if name.startswith(('browser.', 'code.browser.', 'monty.'))]
    assert browser == ['browser.close'], sorted(names)
    (fetch,) = [span for span in spans if span.name == 'web.fetch']
    assert (fetch.attributes or {})['web.site'] == '127.0.0.1'  # the host only
    exported = ' '.join(span.to_json() for span in spans)
    assert f'{site.url}/pharmacy' not in exported and 'Closes at 6pm' not in exported  # no content, no page URL


@pytest.mark.parametrize(
    ('app_env', 'tools'),
    [
        ({}, 'native: none; local: web_fetch'),
        ({'TAVILY_API_KEY': SEARCH_KEY}, 'native: none; local: web_fetch, web_search'),
        ({'MODEL': 'script:e2e.scripts:native_model'}, 'native: web_fetch, web_search; local: none'),
        (
            {'MODEL': 'script:e2e.scripts:native_model', 'NATIVE_WEB_TOOLS': 'false', 'TAVILY_API_KEY': SEARCH_KEY},
            'native: none; local: web_fetch, web_search',
        ),
    ],
    ids=['no-key', 'search-key', 'native', 'native-off'],
)
def test_the_models_own_web_tools_come_first(client: Client, tools: str) -> None:
    client.sign_up()
    assert client.wait_for_reply(client.ask('Which web tools do you have?')) == tools
