"""The web app's Chat apps page (`sammy/static/channels.js`) against local API answers, without a database or model."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from e2e.test_frontend import (
    MockAPI,
    frontend,  # noqa: F401  registers the fixture `chat_apps` builds on
    no_overflow,
    workspace,
)
from playwright.sync_api import Page, Route, expect

CODE = 'ABCDEFGH23'


@pytest.fixture
def chat_apps(request: pytest.FixtureRequest) -> tuple[Page, MockAPI]:
    """The web app against `MockAPI`, under a name that does not shadow the imported fixture."""
    page_and_api: tuple[Page, MockAPI] = request.getfixturevalue('frontend')
    return page_and_api


@dataclass
class Channels:
    enabled: list[str] = field(default_factory=lambda: ['telegram', 'slack'])
    linked: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, str, object]] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split('sammy.test', 1)[1]
        body = request.post_data_json if request.post_data else None
        self.calls.append((request.method, path, body))
        if path == '/api/channels':
            route.fulfill(json={'enabled': self.enabled, 'linked': self.linked})
        elif path == '/api/channels/link':
            assert isinstance(body, dict)
            if body['code'] != CODE:
                route.fulfill(status=404, json={'detail': 'not found'})
                return
            # Linked once the chat sends the code back; the page's list is read again after.
            self.linked = [{'channel': 'telegram', 'notify': True, 'pings': True, 'linked_at': '2026-10-06T10:00:00Z'}]
            route.fulfill(json={'channel': 'telegram', 'code': 'ABCDEFGHJK', 'minutes': 15})
        elif path == '/api/channels/slack/code':
            route.fulfill(json={'code': 'SLACKCODE2', 'minutes': 15})
        elif path == '/api/channels/telegram' and request.method == 'PUT':
            assert isinstance(body, dict)
            self.linked[0]['notify'] = body['notify']
            route.fulfill(json={'ok': True})
        elif path == '/api/channels/telegram' and request.method == 'DELETE':
            self.linked = []
            route.fulfill(json={'ok': True})
        else:
            route.fulfill(status=404, json={'detail': 'not found'})


def install(page: Page) -> Channels:
    channels = Channels()
    page.route('http://sammy.test/api/channels**', channels.handle)
    return channels


@pytest.mark.parametrize('width', [1440, 390])
def test_a_chat_app_is_linked_from_its_link_after_signing_in(chat_apps: tuple[Page, MockAPI], width: int) -> None:
    page, _ = chat_apps
    channels = install(page)
    page.set_viewport_size({'width': width, 'height': 844})
    page.goto(f'http://sammy.test/#/link/{CODE}')
    expect(page.get_by_role('heading', name='Welcome back')).to_be_visible()  # signed out: sign in first
    page.get_by_label('Email address').fill('pat@example.test')
    page.get_by_label('Password', exact=True).fill('correct horse')
    page.click('#signin-button')

    expect(page.locator('#link-card')).to_contain_text('Link this chat app to your Sammy account?')
    expect(page.locator('#title')).to_have_text('Chat apps')
    no_overflow(page)
    page.click('#link-app')
    expect(page.locator('#chat-apps-status')).to_have_text(
        'To finish, send ABCDEFGHJK to Sammy in Telegram within 15 minutes.'
    )
    expect(page.locator('#link-card')).to_be_hidden()
    assert page.evaluate('location.hash') == '#/chat-apps'
    assert ('POST', '/api/channels/link', {'code': CODE}) in channels.calls
    expect(page.locator('#chat-app-list')).to_contain_text('Telegram')
    no_overflow(page)


def test_a_used_link_says_so(chat_apps: tuple[Page, MockAPI]) -> None:
    page, mock = chat_apps
    install(page)
    mock.signed_in = True
    page.goto('http://sammy.test/#/link/ZZZZZZZZZZ')
    page.click('#link-app')
    expect(page.locator('#chat-apps-status')).to_contain_text('That link has expired or was used already.')
    expect(page.locator('#link-card')).to_be_hidden()


def test_pings_unlink_and_a_code_to_send(chat_apps: tuple[Page, MockAPI]) -> None:
    page, mock = chat_apps
    channels = install(page)
    channels.linked = [{'channel': 'telegram', 'notify': True, 'pings': True, 'linked_at': '2026-10-06T10:00:00Z'}]
    workspace(page, mock)
    page.click('#open-chat-apps')
    expect(page.locator('#chat-apps')).to_be_visible()
    expect(page.locator('#open-chat-apps')).to_have_attribute('aria-current', 'page')
    expect(page.locator('#link-card')).to_be_hidden()

    ping = page.get_by_role('button', name='Ping me here: on')
    expect(ping).to_have_attribute('aria-pressed', 'true')
    ping.click()
    expect(page.get_by_role('button', name='Ping me here: off')).to_have_attribute('aria-pressed', 'false')
    assert ('PUT', '/api/channels/telegram', {'notify': False}) in channels.calls

    page.get_by_role('button', name='Get a code').click()  # Slack is on, not linked yet
    expect(page.locator('#chat-apps-status')).to_have_text(
        'Send /start SLACKCODE2 to Sammy in Slack. The code works for 15 minutes, once.'
    )

    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Unlink').click()
    expect(page.get_by_role('button', name='Get a code')).to_have_count(2)
    assert ('DELETE', '/api/channels/telegram', None) in channels.calls
    page.get_by_role('button', name='Back to chat').click()
    expect(page.locator('#composer')).to_be_visible()
