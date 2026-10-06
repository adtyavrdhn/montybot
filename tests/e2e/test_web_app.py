"""#8: the web app, used the way a person uses it: a real (headless) browser on the app's page, clicking and typing.
The bot's own browser is whichever engine the suite runs (`--browser`).

U2 and U3 through the page: sign up, ask for an order, take over the bot's browser in the embedded live view, sign
in with the keyboard, give it back, approve the order.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import App
from playwright.sync_api import Page, expect, sync_playwright
from sites.shop import Shop


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def person() -> Iterator[Page]:
    """The user's own browser, on a laptop-sized screen."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.set_default_timeout(30_000)
        yield page
        browser.close()


def sign_up(page: Page, app: App) -> None:
    page.goto(app.url)
    page.click('#signup-button')
    page.fill('#email', 'pat@example.test')
    page.fill('#password', 'correct horse')
    page.click('#signin-button')
    expect(page.locator('#composer')).to_be_visible()


def send(page: Page, text: str) -> None:
    page.fill('#message', text)
    page.click('#send')


@pytest.mark.scripted
def test_chat(app: App, person: Page) -> None:
    sign_up(person, app)
    send(person, 'Say hello.')
    expect(person.locator('.msg.assistant')).to_have_text('Hello! I am monty-bot.')
    expect(person.locator('#threads li')).to_have_count(1)


@pytest.mark.u2
@pytest.mark.u3
def test_take_over_sign_in_and_approve(app: App, person: Page, shop: Shop) -> None:
    sign_up(person, app)
    send(person, f'Order eggs from {shop.url}')

    person.get_by_role('button', name='Take over the browser').click()
    live = person.frame_locator('#live')
    expect(live.locator('#give-back')).to_be_enabled()
    expect(live.locator('#url')).to_contain_text('/login')
    live.locator('#keyboard').click()
    keys = live.locator('#keys')
    keys.press_sequentially('alice')
    keys.press('Tab')
    keys.press_sequentially('hunter2')
    keys.press('Enter')
    expect(live.locator('#url')).not_to_contain_text('/login')
    live.locator('#give-back').click()

    person.get_by_role('button', name='Approve').click()
    expect(person.locator('.msg.assistant').last).to_contain_text('#1')
    assert [(o.user, o.items) for o in shop.orders] == [('alice', ['eggs'])]


@pytest.mark.scripted
def test_works_on_a_phone(app: App) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        phone = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        sign_up(phone, app)
        send(phone, 'Say hello.')
        expect(phone.locator('.msg.assistant')).to_have_text('Hello! I am monty-bot.')
        phone.click('#menu-button')
        expect(phone.locator('#drawer')).to_have_class('drawer open')
        browser.close()


@pytest.mark.scripted
def test_repeated_enter_creates_one_chat_and_preserves_a_new_draft(app: App, person: Page) -> None:
    sign_up(person, app)
    pending = []
    person.route(
        '**/api/threads', lambda route: pending.append(route) if route.request.method == 'POST' else route.continue_()
    )
    person.fill('#message', 'Say hello.')
    person.evaluate("document.getElementById('composer').requestSubmit()")
    expect(person.locator('#send')).to_be_disabled()
    person.evaluate("document.getElementById('composer').requestSubmit()")
    person.fill('#message', 'My next question')
    assert len(pending) == 1
    pending[0].continue_()
    expect(person.locator('.msg.assistant')).to_have_text('Hello! I am monty-bot.')
    expect(person.locator('#message')).to_have_value('My next question')
    expect(person.locator('#threads li')).to_have_count(1)
