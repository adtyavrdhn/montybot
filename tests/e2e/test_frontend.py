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
    files: list[dict[str, object]] = field(default_factory=list)
    files_truncated: bool = False
    download_status: int = 200
    thread_status: str | None = None
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
            # As the server does: the list's status is the open run's, unless a test sets it on its own.
            running = (
                self.run['status'] if self.run and self.run['status'] in ('queued', 'running', 'waiting') else None
            )
            listed = {'id': THREAD, 'title': 'Compare flights to Lisbon', 'status': self.thread_status or running}
            result = [listed] if self.messages else []
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
        elif path == '/api/files':
            result = {'files': self.files, 'truncated': self.files_truncated, 'max_download_bytes': 20 * 1024 * 1024}
        elif path == '/api/files/download':
            route.fulfill(
                status=self.download_status,
                body='local report',
                content_type='text/plain',
                headers={'Content-Disposition': "attachment; filename*=UTF-8''report%20ready.txt"},
            )
            return
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
        elif path.endswith('/stop'):
            self.messages.append({'role': 'assistant', 'text': 'You stopped this.'})
            self.run = {'id': 'run', 'thread_id': THREAD, 'status': 'stopped', 'activity': [], 'ask': None}
        elif path.endswith('/live'):
            result = {'url': '/mock-live'}
        elif path == '/mock-live':
            back = "parent.postMessage({kind: 'close-takeover'}, location.origin)"  # as the live view's button does
            route.fulfill(
                body=f'<html><body><button onclick="{back}">Back to chat</button></body></html>',
                content_type='text/html',
            )
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
        # Every request stays local, including accidental external assets.
        page.route('**/*', lambda route: route.abort())
        page.route('http://monty.test/**', mock.handle)
        # A controllable SSE transport. Delivering callbacks after close deliberately
        # models an already queued event, so tests exercise the view/run guards.
        page.add_init_script("""
            window.eventSources = [];
            window.EventSource = class {
                static CONNECTING = 0;
                static OPEN = 1;
                static CLOSED = 2;
                constructor(url) {
                    this.url = url;
                    this.closed = false;
                    this.readyState = 1;
                    this.listeners = {};
                    window.eventSources.push(this);
                }
                addEventListener(type, callback) { this.listeners[type] = callback; }
                close() { this.closed = true; this.readyState = 2; }
                emit(type, data) {
                    if (type === 'error') { this.readyState = 0; this.onerror?.(); }  // reconnecting
                    else if (type === 'open') { this.readyState = 1; this.onopen?.(); }
                    else this.listeners[type]?.({data: JSON.stringify(data)});
                }
            };
        """)
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
    expect(page.locator('#enable-notifications')).to_be_hidden()  # this server sends no notifications
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
    mock.schedules = [
        {
            'id': 'task',
            'thread_id': THREAD,
            'name': 'Check shopping prices',
            'when': 'Every Tuesday at 9',
            'paused': False,
        }
    ]
    workspace(page, mock)
    page.click('#open-signins')
    expect(page.locator('#signin-list')).to_contain_text('shop.example.test')
    page.get_by_role('button', name='Forget').click()
    expect(page.locator('#signin-list')).to_contain_text('No saved browser data yet')
    page.click('#open-schedules')
    page.get_by_role('button', name='Pause', exact=True).click()
    expect(page.locator('#schedule-list')).to_contain_text('(paused)')
    page.get_by_role('button', name='Resume', exact=True).click()
    expect(page.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    page.once('dialog', lambda dialog: dialog.accept())  # "Delete ...? Monty will stop running it."
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
        assert ('POST', '/api/runs/run/live', {}) in mock.calls
        expect(page.get_by_role('dialog')).to_be_visible()  # modal: the page behind cannot be reached
        assert page.locator('#takeover').evaluate("(element) => element.matches(':modal')")
        page.frame_locator('#live').get_by_role('button', name='Back to chat').click()
        expect(page.locator('#takeover')).not_to_be_visible()
        expect(page.get_by_role('button', name='Take over the browser')).to_be_focused()
        expect(page.locator('#ask')).to_be_visible()  # the hand-off waits until the browser is given back


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
    page.route(
        '**/api/push/key',
        lambda route: route.fulfill(
            content_type='application/json',
            body='{"public_key":"AQID"}',
        ),
    )
    workspace(page, mock)
    page.click('#enable-notifications')
    expect(page.locator('#enable-notifications')).to_contain_text('Notifications are on')
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


def emit(page: Page, kind: str, data: dict[str, object] | None = None, *, source: int = 0) -> None:
    page.evaluate(
        '([source, kind, data]) => window.eventSources[source].emit(kind, data)',
        [source, kind, data],
    )


def streaming_chat(page: Page, mock: MockAPI) -> None:
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Compare flights'}]
    mock.run = {'id': 'run', 'status': 'running', 'activity': [], 'ask': None}
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#stop')).to_be_visible()
    expect(page.locator('#send')).not_to_be_visible()
    page.wait_for_function('window.eventSources.length === 1')
    assert page.evaluate('window.eventSources[0].url') == '/api/runs/run/events'


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_files_navigation_and_download(frontend: tuple[Page, MockAPI], width: int) -> None:
    page, mock = frontend
    page.set_viewport_size({'width': width, 'height': 844})
    mock.files = [
        {'path': '/work/downloads/report <ready>.txt', 'size': 12},
        {'path': 'large.zip', 'size': 20 * 1024 * 1024 + 1},
    ]
    workspace(page, mock)
    if width < 900:
        page.click('#menu-button')
    page.click('#open-files')
    expect(page.locator('#files')).to_be_visible()
    expect(page.locator('#layout')).not_to_be_visible()
    expect(page.locator('#open-files')).to_have_attribute('aria-current', 'page')
    expect(page.locator('#file-list li')).to_have_count(2)
    expect(page.locator('#file-list li').first).to_have_text('downloads/report <ready>.txt (12 bytes)Download')
    expect(page.locator('#file-list li').last).to_contain_text('large.zip (20.0 MB)')
    expect(page.locator('#file-list ready')).to_have_count(0)
    buttons = page.locator('#file-list').get_by_role('button', name='Download', exact=True)
    expect(buttons.nth(1)).to_be_disabled()
    with page.expect_download() as downloaded:
        buttons.first.click()
    assert downloaded.value.suggested_filename == 'report ready.txt'
    assert ('POST', '/api/files/download', {'path': '/work/downloads/report <ready>.txt'}) in mock.calls
    expect(buttons.first).to_be_enabled()
    no_overflow(page)
    mock.files = []
    page.click('#refresh-files')
    expect(page.locator('#files-status')).to_contain_text('No files yet')
    expect(page.locator('#file-list li')).to_have_count(0)
    mock.files_truncated = True
    page.click('#refresh-files')
    expect(page.locator('#files-status')).to_contain_text('partial list')
    page.locator('#files .back').click()
    expect(page.locator('#composer')).to_be_visible()
    expect(page.locator('#files')).not_to_be_visible()
    expect(page.locator('#send')).to_be_enabled()


@pytest.mark.parametrize(
    ('status', 'message'),
    [(404, 'File unavailable'), (413, '20 MB download limit')],
)
def test_download_errors_are_recoverable(frontend: tuple[Page, MockAPI], status: int, message: str) -> None:
    page, mock = frontend
    mock.files = [{'path': 'report.txt', 'size': 12}]
    mock.download_status = status
    workspace(page, mock)
    page.click('#open-files')
    download = page.locator('#file-list').get_by_role('button', name='Download')
    download.click()
    expect(page.locator('#files-status')).to_contain_text(message)
    expect(download).to_be_enabled()
    expect(page.locator('#files')).to_be_visible()


@pytest.mark.parametrize('width', [1440, 390])
def test_schedule_opens_its_conversation(frontend: tuple[Page, MockAPI], width: int) -> None:
    page, mock = frontend
    page.set_viewport_size({'width': width, 'height': 844})
    mock.messages = [{'role': 'user', 'text': 'Track my flights'}]
    mock.schedules = [
        {'id': 'task', 'thread_id': THREAD, 'name': 'Flight watch', 'when': 'Every Tuesday', 'paused': False}
    ]
    workspace(page, mock)
    if width < 900:
        page.click('#menu-button')
    page.click('#open-schedules')
    page.get_by_role('button', name='Open chat', exact=True).click()
    expect(page).to_have_url(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#schedules')).not_to_be_visible()
    expect(page.locator('#composer')).to_be_visible()
    expect(page.locator('.msg.user')).to_have_text('Track my flights')
    expect(page.locator('#threads button.current')).to_have_attribute('aria-current', 'page')
    assert not any(method in ('POST', 'DELETE') for method, _, _ in mock.calls)
    no_overflow(page)


def test_saved_browser_data_does_not_claim_verified_signin(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.sites = [{'site': 'shop.example.test'}]
    workspace(page, mock)
    expect(page.locator('#open-signins')).to_have_text('Saved browser data')
    page.click('#open-signins')
    expect(page.locator('#signins-title')).to_have_text('Saved browser data')
    expect(page.locator('#signins')).to_contain_text('cookies or storage')
    expect(page.locator('#signins')).to_contain_text('not verified sign-ins')
    expect(page.locator('#signins')).to_contain_text('host and its subdomains')
    expect(page.locator('#signins')).not_to_contain_text('Forget a site to sign out')


def test_sse_snapshots_error_recovery_and_committed_reply(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    emit(page, 'preview', {'revision': 1, 'text': 'First draft', 'activity': 'Finding flights'})
    expect(page.locator('.msg.draft')).to_have_text('First draft')  # no label while the connection is fine
    expect(page.locator('#status')).to_have_text('Finding flights')
    emit(page, 'preview', {'revision': 2, 'text': '<b>Replacement draft</b>', 'activity': 'Comparing fares'})
    expect(page.locator('.msg.assistant')).to_have_count(1)
    expect(page.locator('.msg.assistant')).to_contain_text('<b>Replacement draft</b>')
    expect(page.locator('.msg.assistant b')).to_have_count(0)
    expect(page.locator('#messages')).not_to_contain_text('First draft')
    expect(page.locator('#status')).to_have_text('Comparing fares')
    emit(page, 'preview', {'revision': 'invalid', 'text': 'Bad draft', 'activity': 'Bad activity'})
    expect(page.locator('#messages')).not_to_contain_text('Bad draft')
    emit(page, 'error')
    expect(page.locator('#status')).to_contain_text('Reconnecting')
    expect(page.locator('.msg.assistant')).to_contain_text('Connection lost; this may be incomplete')
    emit(page, 'open')
    expect(page.locator('#status')).to_have_text('Comparing fares')
    expect(page.locator('.msg.draft small')).to_have_count(0)
    mock.messages.append({'role': 'assistant', 'text': 'Committed flight options'})
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'done', 'activity': [], 'ask': None}
    emit(page, 'status', mock.run)
    expect(page.locator('.msg.assistant')).to_have_text('Committed flight options')
    expect(page.locator('#status')).not_to_be_visible()
    expect(page.locator('#send')).to_be_enabled()
    assert page.evaluate('window.eventSources[0].closed')
    emit(page, 'preview', {'revision': 3, 'text': 'Late draft', 'activity': 'Late activity'})
    emit(page, 'error')
    expect(page.locator('.msg.assistant')).to_have_text('Committed flight options')
    assert not any(method == 'POST' for method, _, _ in mock.calls)


@pytest.mark.parametrize('destination', ['#/new', '#/files', '#/schedules', '#/sign-ins'])
def test_navigation_discards_sse_draft_and_late_events(frontend: tuple[Page, MockAPI], destination: str) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    emit(page, 'preview', {'revision': 1, 'text': 'Old draft', 'activity': 'Old activity'})
    page.evaluate('(hash) => { location.hash = hash; }', destination)
    page.wait_for_function('window.eventSources[0].closed')
    emit(page, 'preview', {'revision': 2, 'text': 'Stale draft', 'activity': 'Stale activity'})
    emit(
        page,
        'status',
        {
            'id': 'run',
            'thread_id': THREAD,
            'status': 'waiting',
            'activity': [],
            'ask': {'id': 'stale-ask', 'kind': 'approval', 'prompt': 'Stale approval'},
        },
    )
    emit(page, 'error')
    expect(page.locator('#messages')).not_to_contain_text('Old draft')
    expect(page.locator('#messages')).not_to_contain_text('Stale draft')
    expect(page.locator('#ask')).not_to_be_visible()
    expect(page.locator('#browser')).not_to_be_visible()
    expect(page.locator('#status')).not_to_be_visible()
    expect(page).to_have_url(f'http://monty.test/{destination}')
    if destination != '#/new':
        page.locator('.page:visible .back').click()
        page.wait_for_function('window.eventSources.length === 2')
        assert page.evaluate('!window.eventSources[1].closed')
        emit(page, 'preview', {'revision': 1, 'text': 'Fresh draft', 'activity': 'Fresh activity'}, source=1)
        expect(page.locator('.msg.assistant')).to_contain_text('Fresh draft')


def test_sse_waiting_preserves_answer_and_rejects_wrong_run(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    wrong = {
        'id': 'other-run',
        'thread_id': THREAD,
        'status': 'done',
        'activity': [],
        'ask': None,
    }
    emit(page, 'status', wrong)
    emit(page, 'status', {**wrong, 'id': 'run', 'thread_id': 'other-thread'})
    expect(page.locator('#stop')).to_be_visible()
    assert not page.evaluate('window.eventSources[0].closed')
    waiting = {
        'id': 'run',
        'thread_id': THREAD,
        'status': 'waiting',
        'activity': [],
        'ask': {'id': 'ask', 'kind': 'question', 'prompt': 'Which airport?'},
    }
    emit(page, 'status', waiting)
    answer = page.get_by_label('Your answer to Monty')
    answer.fill('Lisbon')
    emit(page, 'status', waiting)
    expect(answer).to_have_value('Lisbon')
    expect(page.locator('#status')).not_to_be_visible()
    expect(page.locator('#stop')).to_be_visible()
    assert not page.evaluate('window.eventSources[0].closed')


def test_replies_are_formatted_and_safe(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [
        {'role': 'user', 'text': 'Top stories, **please**'},
        {
            'role': 'assistant',
            'text': 'Here are the **top 2**:\n\n1. *First* story\n2. `second` story\n\n'
            '| Title | Points |\n|---|---|\n| One | 410 |\n\n'
            'More at https://news.example.test/top. [Bad](javascript:alert(1)) <img src=x onerror=alert(1)>\n\n'
            '3. Third, see [Python](https://en.wikipedia.org/wiki/Python_(programming_language)).\n\n'
            '````\nshow ``` in code\n````',
        },
    ]
    page.goto(f'http://monty.test/#/t/{THREAD}')
    reply = page.locator('.msg.assistant')
    expect(reply.locator('strong')).to_have_text('top 2')
    expect(reply.locator('ol').first.locator('li')).to_have_count(2)
    expect(reply.locator('em')).to_have_text('First')
    expect(reply.locator('li code')).to_have_text('second')
    expect(reply.locator('td').first).to_have_text('One')
    expect(reply.locator('a')).to_have_count(2)
    expect(reply.locator('a').first).to_have_attribute('href', 'https://news.example.test/top')
    expect(reply.locator('a').last).to_have_attribute(
        'href', 'https://en.wikipedia.org/wiki/Python_(programming_language)'
    )
    expect(reply.locator('ol').last).to_have_attribute('start', '3')
    expect(reply.locator('pre code')).to_have_text('show ``` in code')  # a shorter fence does not close it
    expect(reply).to_contain_text('[Bad](javascript:alert(1)) <img src=x onerror=alert(1)>')
    expect(reply.locator('img')).to_have_count(0)
    expect(page.locator('.msg.user')).to_have_text('Top stories, **please**')  # the user's words, as typed


def test_stop_a_run(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    page.get_by_role('button', name='Stop', exact=True).click()
    expect(page.locator('.msg.assistant')).to_have_text('You stopped this.')
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('#stop')).not_to_be_visible()
    assert ('POST', '/api/runs/run/stop', {}) in mock.calls
    assert page.evaluate('window.eventSources[0].closed')


def test_chat_list_shows_which_chats_need_you(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.messages = [{'role': 'user', 'text': 'Order eggs'}]
    mock.thread_status = 'waiting'
    workspace(page, mock)
    expect(page.locator('#threads .badge')).to_have_text('Needs you')
    mock.thread_status = None
    page.click('#open-files')  # any navigation reloads the list
    expect(page.locator('#threads .badge')).to_have_count(0)


def test_messages_carry_the_time_zone_and_failures_show_in_the_page(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    workspace(page, mock)
    timezone = page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')
    page.fill('#message', 'Compare flights')
    page.click('#send')
    expect(page.locator('.msg.assistant')).to_have_text('Here are the options.')
    assert ('POST', '/api/threads', {'text': 'Compare flights', 'timezone': timezone}) in mock.calls
    page.route(
        '**/api/threads/*/messages',
        lambda route: route.fulfill(status=409, content_type='application/json', body='{"detail":"still working"}'),
    )
    page.on('dialog', lambda dialog: pytest.fail(f'unexpected alert: {dialog.message}'))
    page.fill('#message', 'And hotels')
    page.click('#send')
    expect(page.locator('#notice')).to_contain_text('still working')
    expect(page.locator('#message')).to_have_value('And hotels')  # nothing typed is lost
    page.get_by_role('button', name='Dismiss').click()
    expect(page.locator('#notice')).not_to_be_visible()


def test_a_scheduled_tasks_chat_before_its_first_run_says_so(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#messages')).to_contain_text('Nothing here yet')
    expect(page.locator('#send')).to_be_enabled()


def test_skip_link_and_a_working_chat_say_where_you_are(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    expect(page.locator('#message')).to_have_attribute('placeholder', re.compile('Monty is on it'))
    page.keyboard.press('Tab')  # the skip link is the first thing on the page
    page.keyboard.press('Enter')
    expect(page.locator('#message')).to_be_focused()
    expect(page).to_have_url(f'http://monty.test/#/t/{THREAD}')  # still in the chat


def test_enter_while_monty_waits_goes_to_the_question(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Order eggs'}]
    mock.run = {
        'id': 'run',
        'status': 'waiting',
        'activity': [],
        'ask': {'id': 'ask', 'kind': 'question', 'prompt': 'Brown or white?'},
    }
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#message')).to_have_attribute('placeholder', re.compile('waiting for you'))
    page.fill('#message', 'brown')
    page.press('#message', 'Enter')
    expect(page.get_by_label('Your answer to Monty')).to_be_focused()
    assert not any(method == 'POST' for method, _, _ in mock.calls)


def test_the_chat_list_keeps_focus_and_marks_no_chat_on_other_pages(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.messages = [{'role': 'user', 'text': 'Order eggs'}]
    workspace(page, mock)
    page.locator('#threads button').first.click()
    expect(page.locator('#threads button.current')).to_have_count(1)
    page.locator('#threads button').first.focus()
    mock.thread_status = 'waiting'
    page.evaluate('loadThreads()')  # as the 15-second refresh does, with a new badge
    expect(page.locator('#threads .badge')).to_have_text('Needs you')
    expect(page.locator('#threads button').first).to_be_focused()
    page.click('#open-files')
    expect(page.locator('#threads button.current')).to_have_count(0)


def test_inline_triple_backticks_do_not_swallow_the_reply(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [
        {'role': 'user', 'text': 'How do I install it?'},
        {'role': 'assistant', 'text': 'Run ```npm install``` first.\n\nThen **start** it.'},
    ]
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('.msg.assistant pre')).to_have_count(0)
    expect(page.locator('.msg.assistant strong')).to_have_text('start')


def test_a_run_started_elsewhere_shows_in_the_open_chat(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Weekly order check'}]
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#send')).to_be_visible()
    mock.thread_status = 'running'  # a schedule started it
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'running', 'activity': ['Opening shop.test'], 'ask': None}
    page.evaluate('loadThreads()')  # as the 15-second refresh does
    expect(page.locator('#stop')).to_be_visible()
    expect(page.locator('#status')).to_have_text('Opening shop.test')


def test_sending_to_a_deleted_chat_starts_over_and_says_why(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Weekly order check'}]
    page.goto(f'http://monty.test/#/t/{THREAD}')
    page.route(
        '**/api/threads/*/messages',
        lambda route: route.fulfill(status=404, content_type='application/json', body='{"detail":"not found"}'),
    )
    page.fill('#message', 'Run it now')
    page.click('#send')
    expect(page).to_have_url('http://monty.test/#/new')
    expect(page.locator('#notice')).to_contain_text('That chat was deleted')
    expect(page.locator('#message')).to_have_value('Run it now')


def test_a_chat_that_fails_to_load_says_so_and_lets_you_act(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    mock.messages = [{'role': 'user', 'text': 'Order eggs'}]
    page.route(
        f'**/api/threads/{THREAD}', lambda route: route.fulfill(status=500, body='{}', content_type='application/json')
    )
    page.goto(f'http://monty.test/#/t/{THREAD}')
    expect(page.locator('#title')).to_have_text('Could not load this chat')
    expect(page.locator('#notice')).to_contain_text('Something went wrong (500)')
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('#threads button')).to_have_count(1)  # the list loads anyway, to try again from


def test_no_connection_says_so_in_plain_words(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    workspace(page, mock)
    page.route('**/api/threads', lambda route: route.abort())
    page.fill('#message', 'Compare flights')
    page.click('#send')
    expect(page.locator('#notice-text')).to_have_text('Could not reach Monty. Check your connection, and try again.')


def test_a_chat_whose_stream_closed_for_good_catches_up_from_the_list(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    streaming_chat(page, mock)
    page.evaluate('window.eventSources[0].close()')  # as an HTTP error (a 502 in a restart) closes it for good
    mock.messages.append({'role': 'assistant', 'text': 'All done.'})
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'done', 'activity': [], 'ask': None}
    page.evaluate('loadThreads()')  # as the 15-second refresh does
    expect(page.locator('.msg.assistant')).to_have_text('All done.')
    expect(page.locator('#send')).to_be_visible()


def test_offline_at_start_says_so_instead_of_looking_signed_out(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.signed_in = True
    page.route('**/api/me', lambda route: route.abort())
    page.goto('http://monty.test/')
    expect(page.locator('#signin-error')).to_have_text('Could not reach Monty. Check your connection, and try again.')


def test_background_refreshes_stay_quiet_offline(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    workspace(page, mock)
    page.route('**/api/threads', lambda route: route.abort())
    page.evaluate('reportUnlessOffline(loadThreads())')  # as the 15-second refresh does
    page.wait_for_timeout(300)
    expect(page.locator('#notice')).to_be_hidden()


def test_a_sign_in_the_browser_does_not_keep_is_explained(frontend: tuple[Page, MockAPI]) -> None:
    page, _ = frontend
    page.goto('http://monty.test/')
    # Signed in, but the session cookie is not kept, so /api/me still answers 401.
    page.route('**/api/signin', lambda route: route.fulfill(status=200, content_type='application/json', body='{}'))
    page.fill('#email', 'pat@example.test')
    page.fill('#password', 'correct horse')
    page.click('#signin-button')
    expect(page.locator('#signin-error')).to_contain_text('Allow cookies for this site')
