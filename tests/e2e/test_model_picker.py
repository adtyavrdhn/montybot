"""Web model preferences against the shared API envelope, without a database or model."""

from dataclasses import dataclass, field

import pytest
from e2e.test_frontend import (
    MockAPI,
    frontend,  # noqa: F401  registers the fixture `picker` builds on
    no_overflow,
    workspace,
)
from playwright.sync_api import Page, Route, expect


@pytest.fixture
def picker(request: pytest.FixtureRequest) -> tuple[Page, MockAPI]:
    """The web app against `MockAPI`, under a name that does not shadow the imported fixture."""
    page_and_api: tuple[Page, MockAPI] = request.getfixturevalue('frontend')
    return page_and_api


MODELS: list[dict[str, object]] = [
    {
        'id': 'anthropic:claude',
        'name': 'Claude',
        'thinking': ['low', 'medium', 'high'],
        'options': {'thinking': ['low', 'medium', 'high'], 'service_tier': ['auto', 'standard']},
        'defaults': {'thinking': 'medium'},
    },
    {
        'id': 'openai:gpt',
        'name': 'GPT',
        'thinking': ['low', 'medium', 'high'],
        'options': {'thinking': ['low', 'medium', 'high', 'xhigh'], 'verbosity': ['low', 'medium', 'high']},
        'defaults': {'thinking': 'medium', 'verbosity': 'low'},
    },
    {'id': 'test:plain', 'name': 'Plain', 'thinking': [], 'options': {}, 'defaults': {}},
]


@dataclass
class Preferences:
    model: str = 'anthropic:claude'
    settings: dict[str, object] = field(default_factory=dict)
    writes: list[object] = field(default_factory=list)
    status: int = 200
    hold: bool = False
    pending: list[Route] = field(default_factory=list)

    def envelope(self) -> dict[str, object]:
        return {'model': self.model, 'settings': self.settings, 'models': MODELS}

    def handle(self, route: Route) -> None:
        if self.hold:
            self.pending.append(route)
            return
        if self.status != 200:
            route.fulfill(status=self.status, json={'detail': 'Preferences are unavailable.'})
            return
        if route.request.method == 'PUT':
            body = route.request.post_data_json
            assert isinstance(body, dict)
            self.writes.append(body)
            self.model = body['model']
            self.settings = body['settings']
        route.fulfill(json=self.envelope())


def install(page: Page) -> Preferences:
    preferences = Preferences()
    page.route('http://sammy.test/api/model-preferences', preferences.handle)
    return preferences


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_settings_and_quick_picker_persist(picker: tuple[Page, MockAPI], width: int) -> None:
    page, mock = picker
    prefs = install(page)
    page.set_viewport_size({'width': width, 'height': 844})
    workspace(page, mock)
    quick = page.locator('#quick-model')
    expect(quick.get_by_label('Model', exact=True)).to_have_value('anthropic:claude')
    expect(quick.get_by_label('Thinking', exact=True)).to_have_value('')
    no_overflow(page)
    quick.get_by_label('Thinking', exact=True).select_option('high')
    expect(quick.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {'thinking': 'high'}
    quick.get_by_role('link', name='Model settings').click()
    settings = page.locator('#settings-model')
    expect(settings.get_by_label('Thinking', exact=True)).to_have_value('high')
    expect(settings.get_by_label('Temperature', exact=True)).not_to_be_visible()
    settings.get_by_text('Advanced', exact=True).click()
    settings.get_by_label('service tier', exact=True).select_option('standard')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {'thinking': 'high', 'service_tier': 'standard'}
    settings.get_by_label('Model', exact=True).select_option('openai:gpt')
    expect(settings.get_by_label('Model', exact=True)).to_have_value('openai:gpt')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {}  # do not leak Claude overrides to GPT
    expect(settings.get_by_label('service tier', exact=True)).to_have_count(0)
    expect(settings.get_by_label('All thinking levels')).to_be_visible()
    settings.get_by_label('All thinking levels').select_option('xhigh')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {'thinking': 'xhigh'}
    settings.get_by_label('Maximum output tokens').fill('512')
    settings.get_by_label('Maximum output tokens').press('Tab')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings['max_tokens'] == 512
    no_overflow(page)
    page.reload()
    expect(settings.get_by_label('Model', exact=True)).to_have_value('openai:gpt')
    page.locator('#settings .back').click()
    expect(quick.get_by_label('Model', exact=True)).to_have_value('openai:gpt')
    quick.get_by_label('Thinking', exact=True).select_option('low')
    expect(quick.get_by_role('status')).to_have_text('Saved for your next run.')
    page.locator('#message').fill('Use my saved model')
    page.locator('#send').click()
    expect(page.locator('.msg.user')).to_have_text('Use my saved model')
    assert prefs.model == 'openai:gpt'
    assert prefs.settings == {'thinking': 'low', 'max_tokens': 512}
    assert (
        'POST',
        '/api/threads',
        {'text': 'Use my saved model', 'timezone': page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')},
    ) in mock.calls


def test_unsupported_options_and_numeric_validation(picker: tuple[Page, MockAPI]) -> None:
    page, mock = picker
    prefs = install(page)
    workspace(page, mock)
    page.locator('#quick-model a').click()
    settings = page.locator('#settings-model')
    settings.get_by_label('Model', exact=True).select_option('test:plain')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    expect(settings.get_by_label('Thinking', exact=True)).to_have_count(0)
    expect(page.locator('#quick-model').get_by_label('Thinking', exact=True)).to_have_count(0)
    settings.get_by_text('Advanced', exact=True).click()
    tokens = settings.get_by_label('Maximum output tokens')
    tokens.fill('0')
    tokens.press('Tab')
    assert not tokens.evaluate('(input) => input.validity.valid')
    assert prefs.settings == {}
    tokens.fill('256')
    tokens.press('Tab')
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {'max_tokens': 256}
    settings.get_by_role('button', name='Reset to model defaults').click()
    expect(settings.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.settings == {}


def test_load_and_save_failures_and_retry(picker: tuple[Page, MockAPI]) -> None:
    page, mock = picker
    prefs = install(page)
    prefs.status = 503
    workspace(page, mock)
    quick = page.locator('#quick-model')
    expect(quick.get_by_role('alert')).to_contain_text('Could not load models')
    expect(quick.get_by_label('Model', exact=True)).to_have_count(0)
    prefs.status = 200
    quick.get_by_role('button', name='Reload models').click()
    expect(quick.get_by_label('Model', exact=True)).to_have_value('anthropic:claude')
    prefs.status = 422
    quick.get_by_label('Model', exact=True).select_option('openai:gpt')
    expect(quick.get_by_role('alert')).to_contain_text('Could not save')
    expect(quick.get_by_label('Model', exact=True)).to_have_value('anthropic:claude')
    assert prefs.model == 'anthropic:claude'
    prefs.status = 200
    quick.get_by_label('Model', exact=True).select_option('openai:gpt')
    expect(quick.get_by_role('status')).to_have_text('Saved for your next run.')
    assert prefs.model == 'openai:gpt'


def test_send_waits_for_save(picker: tuple[Page, MockAPI]) -> None:
    page, mock = picker
    prefs = install(page)
    workspace(page, mock)
    quick = page.locator('#quick-model')
    expect(quick.get_by_label('Model', exact=True)).to_have_value('anthropic:claude')
    prefs.hold = True
    quick.get_by_label('Model', exact=True).select_option('openai:gpt')
    expect(quick.get_by_role('status')).to_have_text('Saving model settings…')
    expect(quick.get_by_label('Model', exact=True)).to_be_disabled()
    page.locator('#message').fill('Next run')
    page.locator('#send').click()
    expect(page.locator('#notice')).to_contain_text('Wait for model settings')
    assert not any(method == 'POST' and path == '/api/threads' for method, path, _ in mock.calls)
    prefs.hold = False
    assert len(prefs.pending) == 1
    prefs.handle(prefs.pending.pop())
    expect(quick.get_by_role('status')).to_have_text('Saved for your next run.')
    page.locator('#send').click()
    expect(page.locator('.msg.user')).to_have_text('Next run')
    assert prefs.model == 'openai:gpt'
