"""#8: the web app, used the way a person uses it: a real (headless) browser on the app's page, clicking and typing.
The bot's own browser is whichever engine the suite runs (`--browser`).

U2 and U3 through the page: sign up, ask for an order, take over the bot's browser in the embedded live view, sign
in with the keyboard, give it back, approve the order.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import App
from PIL import Image
from playwright.sync_api import FloatRect, Page, expect, sync_playwright
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


def assert_fits_the_window(page: Page, selector: str) -> FloatRect:
    """The element is wholly on screen: nothing of it needs scrolling to."""
    box = page.frame_locator('#live').locator(selector).bounding_box()
    viewport = page.viewport_size
    assert box is not None and viewport is not None
    assert box['x'] >= 0 and box['y'] >= 0
    assert box['x'] + box['width'] <= viewport['width'] and box['y'] + box['height'] <= viewport['height']
    return box


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
    live.get_by_role('button', name='Back to chat').click()  # not yet: the hand-off waits
    expect(person.locator('#takeover')).to_be_hidden()
    person.get_by_role('button', name='Take over the browser').click()
    expect(live.locator('#give-back')).to_be_enabled()
    expect(live.locator('#url')).to_contain_text('/login')
    assert_fits_the_window(person, '#view')
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


def test_take_over_on_a_phone(app: App, shop: Shop, request: pytest.FixtureRequest) -> None:
    """On a phone the bot's browser fills the screen's width, laid out at the phone's size when the engine can."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        phone = browser.new_page(
            viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, device_scale_factor=3
        )
        phone.set_default_timeout(30_000)
        sign_up(phone, app)
        send(phone, f'Order eggs from {shop.url}')
        phone.get_by_role('button', name='Take over the browser').click()
        live = phone.frame_locator('#live')
        expect(live.locator('#reason')).to_contain_text('Monty needs you:')
        expect(live.locator('#url')).to_contain_text('/login')
        if request.config.getoption('--browser') == 'chromium':
            expect(live.locator('#view')).to_have_attribute('width', re.compile(r'^3\d\d$'))  # phone-sized frames
            assert assert_fits_the_window(phone, '#view')['width'] >= 350
        else:
            assert_fits_the_window(phone, '#view')  # HtmlBrowser cannot resize: its picture is scaled down
        live.locator('#give-back').click()
        expect(phone.locator('#takeover')).to_be_hidden()
        browser.close()


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


@pytest.mark.scripted
def test_attach_files_and_see_them_in_the_chat(app: App, person: Page) -> None:
    sign_up(person, app)
    picture = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 30, 120)).save(picture, format='PNG')
    person.set_input_files(
        '#file-input',
        files=[
            {'name': 'receipt.png', 'mimeType': 'image/png', 'buffer': picture.getvalue()},
            {'name': 'totals.csv', 'mimeType': 'text/csv', 'buffer': b'item,total\neggs,3\n'},
        ],
    )
    expect(person.locator('#attachments li')).to_have_count(2)
    expect(person.locator('#attachments li').last).to_contain_text('Monty reads it')  # uploaded
    send(person, 'Describe what I attached')
    reply = person.locator('.msg.assistant')
    expect(reply).to_contain_text('image/png 64x48')
    expect(reply).to_contain_text('file text: <file name="totals.csv">')
    image = person.locator('.msg.user a.file-image img')
    expect(image).to_have_attribute('alt', 'receipt.png')
    person.wait_for_function('document.querySelector(".msg.user a.file-image img").naturalWidth === 64')
    with person.expect_download() as pending:
        person.locator('.msg.user a.file-card').click()
    saved = pending.value.path()
    assert pending.value.suggested_filename == 'totals.csv' and Path(saved).read_bytes() == b'item,total\neggs,3\n'
