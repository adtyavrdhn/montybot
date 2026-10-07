"""Frontend contracts with local API responses. No database, model, or external sites."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright

STATIC = Path(__file__).resolve().parents[2] / 'montybot' / 'static'
THREAD = '11111111-1111-1111-1111-111111111111'


@dataclass(kw_only=True)
class MockAPI:
    signed_in: bool = False
    messages: list[dict[str, str]] = field(default_factory=list)
    run: dict[str, object] | None = None
    sites: list[dict[str, str]] = field(default_factory=list)
    schedules: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, str, object]] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split('monty.test', 1)[1]
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.calls.append((method, path, body))
        if path == '/':
            route.fulfill(path=str(STATIC / 'index.html'), content_type='text/html')
            return
        if path.startswith('/static/'):
            file = STATIC / path.removeprefix('/static/')
            types = {'.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json'}
            route.fulfill(path=str(file), content_type=types.get(file.suffix, 'text/plain'))
            return
        status = 200
        result: object = {}
        if path == '/api/me':
            status = 200 if self.signed_in else 401
        elif path in ('/api/signin', '/api/signup'):
            self.signed_in = True
            status = 201 if path == '/api/signup' else 200
        elif path == '/api/threads' and method == 'GET':
            result = [{'id': THREAD, 'title': 'Compare flights to Lisbon'}] if self.messages else []
        elif path == '/api/threads' and method == 'POST':
            assert isinstance(body, dict)
            self.messages = [
                {'role': 'user', 'text': body['text']},
                {'role': 'assistant', 'text': 'Here are the options.'},
            ]
            result = {'thread_id': THREAD}
            status = 201
        elif path == f'/api/threads/{THREAD}':
            result = {'title': 'Compare flights to Lisbon', 'messages': self.messages, 'run': self.run}
        elif path == '/api/sign-ins':
            result = self.sites
        elif path.startswith('/api/sign-ins/') and method == 'DELETE':
            self.sites = []
        elif path == '/api/schedules':
            result = self.schedules
        elif path.startswith('/api/schedules/'):
            if method == 'DELETE':
                self.schedules = []
            else:
                self.schedules[0]['paused'] = path.endswith('/pause')
        elif path.startswith('/api/asks/'):
            self.run = None
        elif path.endswith('/live'):
            result = {'url': '/mock-live'}
        elif path == '/mock-live':
            route.fulfill(body='<html><body><button>Give browser back</button></body></html>', content_type='text/html')
            return
        elif path.endswith('/screen'):
            route.fulfill(
                body='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="#fff"/><text x="40" y="60" font-size="24">Browser preview</text></svg>',
                content_type='image/svg+xml',
            )
            return
        elif path == '/api/push/key':
            result = {'public_key': None}
        elif path == '/api/push/subscriptions':
            result = {}
        elif path == '/api/signout':
            self.signed_in = False
        else:
            status = 404
        route.fulfill(status=status, body=json.dumps(result), content_type='application/json')


@pytest.fixture
def frontend() -> Iterator[tuple[Page, MockAPI]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        mock = MockAPI()
        page.route('http://monty.test/**', mock.handle)
        yield page, mock
        browser.close()


def workspace(page: Page, mock: MockAPI) -> None:
    mock.signed_in = True
    page.goto('http://monty.test/')
    expect(page.locator('#composer')).to_be_visible()


def no_overflow(page: Page) -> None:
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_auth_and_signup(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.goto('http://monty.test/')
    expect(page.get_by_role('heading', name='Welcome back')).to_be_visible()
    page.click('#signup-button')
    expect(page.locator('#auth-title')).to_have_text('Make room for Monty')
    expect(page.locator('#password')).to_have_attribute('autocomplete', 'new-password')
    page.get_by_label('Email address').fill('pat@example.test')
    page.get_by_label('Password', exact=True).fill('correct horse')
    page.click('#signin-button')
    expect(page.locator('#composer')).to_be_visible()
    assert ('POST', '/api/signup', {'email': 'pat@example.test', 'password': 'correct horse'}) in mock.calls
    page.click('#signout')
    expect(page.locator('#signin-form')).to_be_visible()


@pytest.mark.parametrize('width', [1440, 1024, 768, 390, 320])
def test_responsive_navigation_and_prompts(frontend: tuple[Page, MockAPI], width: int) -> None:
    page, mock = frontend
    page.set_viewport_size({'width': width, 'height': 844})
    workspace(page, mock)
    no_overflow(page)
    page.get_by_role('button', name='Find something worth the trip').click()
    expect(page.locator('#message')).to_have_value('Find the three cheapest flights to Lisbon next Friday.')
    assert not any(method == 'POST' for method, _, _ in mock.calls)
    if width < 900:
        expect(page.locator('#drawer')).not_to_be_visible()
        page.click('#menu-button')
        expect(page.locator('#menu-button')).to_have_attribute('aria-expanded', 'true')
        expect(page.locator('#close-drawer')).to_be_focused()
        page.keyboard.press('Shift+Tab')
        expect(page.locator('#signout')).to_be_focused()
        page.keyboard.press('Tab')
        expect(page.locator('#close-drawer')).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.locator('#menu-button')).to_be_focused()
        expect(page.locator('#drawer')).not_to_be_visible()
        page.click('#menu-button')
    else:
        expect(page.locator('#drawer')).to_be_visible()
        expect(page.locator('#menu-button')).not_to_be_visible()
    page.click('#open-schedules')
    expect(page.locator('#schedules')).to_be_visible()
    expect(page.locator('#schedules-title')).to_be_focused()
    expect(page.locator('#layout')).not_to_be_visible()
    no_overflow(page)
    page.get_by_role('button', name='Back to chat').click()
    expect(page.locator('#composer')).to_be_visible()
    expect(page.locator('#send')).to_be_enabled()
    page.fill('#message', 'Compare flights')
    page.click('#send')
    expect(page.locator('.msg.assistant')).to_have_text('Here are the options.')
    expect(page.locator('#threads button.current')).to_have_attribute('aria-current', 'page')
    no_overflow(page)


def test_saved_signins_and_schedules(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.sites = [{'site': 'shop.example.test'}]
    mock.schedules = [{'id': 'task', 'name': 'Check shopping prices', 'when': 'Every Tuesday at 9', 'paused': False}]
    workspace(page, mock)
    page.click('#open-signins')
    expect(page.locator('#signin-list')).to_contain_text('shop.example.test')
    page.get_by_role('button', name='Forget').click()
    expect(page.locator('#signin-list')).to_contain_text('No saved sign-ins yet')
    page.click('#open-schedules')
    page.get_by_role('button', name='Pause', exact=True).click()
    expect(page.locator('#schedule-list')).to_contain_text('(paused)')
    page.get_by_role('button', name='Resume', exact=True).click()
    expect(page.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    page.get_by_role('button', name='Delete', exact=True).click()
    expect(page.locator('#schedule-list')).to_contain_text('No scheduled tasks yet')
    assert ('DELETE', '/api/sign-ins/shop.example.test', None) in mock.calls
    assert ('POST', '/api/schedules/task/pause', {}) in mock.calls
    assert ('POST', '/api/schedules/task/resume', {}) in mock.calls
    assert ('DELETE', '/api/schedules/task', None) in mock.calls


@pytest.mark.parametrize('kind', ['question', 'approval', 'handoff'])
def test_asks(frontend: tuple[Page, MockAPI], kind: str) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Order eggs'}]
    mock.run = {
        'id': 'run',
        'status': 'waiting',
        'activity': [],
        'ask': {'id': 'ask', 'kind': kind, 'prompt': 'Please review this step.'},
    }
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#ask')).to_contain_text('Please review this step.')
    if kind == 'question':
        page.get_by_label('Your answer to Monty').fill('Two boxes')
        page.get_by_role('button', name='Answer', exact=True).click()
        assert ('POST', '/api/asks/ask', {'text': 'Two boxes'}) in mock.calls
    elif kind == 'approval':
        page.get_by_role('button', name='Approve', exact=True).click()
        assert ('POST', '/api/asks/ask', {'approved': True}) in mock.calls
    else:
        page.get_by_role('button', name='Take over the browser', exact=True).click()
        expect(page.locator('#live')).to_be_visible()
        expect(page.locator('#browser-label')).to_contain_text('You have the browser')
        assert ('POST', '/api/runs/run/live', {}) in mock.calls
        page.click('#close-browser')
        expect(page.locator('#browser')).not_to_be_visible()


@pytest.mark.parametrize('width', [1440, 390])
def test_browser_watch(frontend: tuple[Page, MockAPI], width: int) -> None:
    page, mock = frontend
    page.set_viewport_size({'width': width, 'height': 844})
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Look up flights'}]
    mock.run = {'id': 'run', 'status': 'running', 'activity': ['Comparing flights'], 'ask': None}
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#status')).to_have_text('Comparing flights')
    if width < 900:
        page.click('#browser-button')
        assert page.locator('#chat').evaluate('(element) => element.inert')
    expect(page.locator('#browser')).to_be_visible()
    expect(page.locator('#screen')).to_have_attribute('src', re.compile(r'blob:.*'))
    no_overflow(page)
    page.click('#close-browser')
    expect(page.locator('#browser-button')).to_be_visible()
    if width < 900:
        page.click('#menu-button')
    page.click('#new-chat')
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('#browser')).not_to_be_visible()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_auth_errors_and_small_screens(frontend: tuple[Page, MockAPI], width: int) -> None:
    page, _ = frontend
    page.set_viewport_size({'width': width, 'height': 568})
    page.goto('http://monty.test/')
    page.route(
        '**/api/signin',
        lambda route: route.fulfill(
            status=401,
            content_type='application/json',
            body='{"detail":"Email or password is incorrect."}',
        ),
    )
    page.fill('#email', 'pat@example.test')
    page.fill('#password', 'incorrect password')
    page.click('#signin-button')
    expect(page.get_by_role('alert')).to_have_text('Email or password is incorrect.')
    expect(page.locator('#signin-button')).to_be_enabled()
    no_overflow(page)
    page.click('#signup-button')
    expect(page.get_by_role('alert')).to_be_empty()
    expect(page.locator('#email')).to_be_focused()


def test_notification_opt_in_and_signout(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    # Local Push API double: no browser permission prompts or external push services.
    page.add_init_script("""
        window.Notification = {requestPermission: async () => 'granted', permission: 'granted'};
        window.PushManager = function () {};
        const subscription = {
            endpoint: 'https://push.example.test/local',
            toJSON: () => ({endpoint: 'https://push.example.test/local', keys: {p256dh: 'local', auth: 'local'}}),
            unsubscribe: async () => {},
        };
        const registration = {pushManager: {
            subscribe: async () => subscription,
            getSubscription: async () => subscription,
        }};
        Object.defineProperty(navigator, 'serviceWorker', {value: {
            register: async (url) => { window.registeredWorker = url; return registration; },
            getRegistration: async () => registration,
        }});
    """)
    workspace(page, mock)
    page.route(
        '**/api/push/key',
        lambda route: route.fulfill(
            content_type='application/json',
            body='{"public_key":"AQID"}',
        ),
    )
    page.click('#enable-notifications')
    expect(page.locator('#enable-notifications')).to_have_text('Notifications are on')
    assert page.evaluate('window.registeredWorker') == '/sw.js'
    assert (
        'POST',
        '/api/push/subscriptions',
        {
            'endpoint': 'https://push.example.test/local',
            'keys': {'p256dh': 'local', 'auth': 'local'},
        },
    ) in mock.calls
    page.click('#signout')
    expect(page.locator('#signin-form')).to_be_visible()
    assert ('DELETE', '/api/push/subscriptions', {'endpoint': 'https://push.example.test/local'}) in mock.calls
