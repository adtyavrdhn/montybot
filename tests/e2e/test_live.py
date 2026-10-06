"""Nightly: the user paths against real sites, with a real model and a real browser.

    MONTYBOT_TEST_MODEL=anthropic:claude-sonnet-4-5 uv run pytest tests/e2e/test_live.py --live --browser=chromium

Real sites change and challenge datacenter addresses, so these assert only what a user would accept as done, and a
failure is a signal to look, not a bug by itself. Bot-check results per engine belong to #16.
"""

from __future__ import annotations

import pytest
from conftest import Client
from helpers import eventually

pytestmark = pytest.mark.live


@pytest.mark.u1
def test_live_read_the_web(client: Client) -> None:
    client.sign_up()
    thread = client.ask(
        'Open https://news.ycombinator.com/ and tell me the titles of the top three stories, as a numbered list.'
    )
    reply = client.wait_for_reply(thread)
    assert reply.count('\n') >= 2 and '1' in reply


@pytest.mark.u1
def test_live_shop_search(client: Client) -> None:
    client.sign_up()
    thread = client.ask(
        'Search https://www.walmart.com/search?q=oat+milk and tell me the cheapest oat milk and its price. '
        'If a check asks you to press and hold, hand me the browser.'
    )

    def outcome() -> str | None:
        run = client.thread(thread)['run']
        if run['ask'] is not None and run['ask']['kind'] == 'handoff':
            return 'challenged'  # a bot check; nobody is there to solve it at night (see #16)
        if run['status'] in ('done', 'failed'):
            return client.thread(thread)['messages'][-1]['text']
        return None

    result = eventually(outcome, timeout=180, what='a reply or a hand-off')
    assert result == 'challenged' or '$' in result


@pytest.mark.u2
def test_live_sign_in_is_handed_to_the_user(client: Client) -> None:
    client.sign_up()
    thread = client.ask('Check my GitHub notifications at https://github.com/notifications.')
    handoff = client.wait_for_ask(thread, 'handoff')
    assert handoff['prompt']
