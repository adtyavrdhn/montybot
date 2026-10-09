"""#128: a lesson in the live view ("Teach Sammy") records what the user does, and never what they type into a
password or other sensitive field: not as text, not as key presses, not in a page's summary. Unit tests of `Lesson`,
then a lesson over the live view's real WebSocket with `FakeBrowser`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest
from liveview_harness import StubBrowserService, serve_app

from sammy.browser.contract import Click, MouseDown, MouseUp, Navigate, Point, Press, Scroll, Type
from sammy.browser.fake import FakeBrowser, FakeElement, FakePage
from sammy.browser.live import Outline, OutlineItem, Tab, Tabs
from sammy.browser.service import UserId
from sammy.liveview.app import live_view_app
from sammy.liveview.auth import StubAuthenticator
from sammy.liveview.client import LiveViewClient
from sammy.liveview.handoffs import InMemoryHandoffs
from sammy.liveview.recording import Drafted, Lesson, TeachingFailed, is_secret
from sammy.liveview.wire import TeachRequest, TeachStop

pytestmark = pytest.mark.anyio

SECRET = 'hunter2-Sw0rdf1sh'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def field(name: str, *, role: str = 'textbox', focused: bool = False, secure: bool = False) -> OutlineItem:
    return OutlineItem(role=role, name=name, x=0, y=0, width=0, height=0, focused=focused, secure=secure)


class Page:
    """An outline whose focus the test moves, as the page would."""

    def __init__(self, *items: OutlineItem, available: bool = True) -> None:
        self.items = list(items)
        self.available = available

    def focus(self, name: str) -> None:
        self.items = [
            OutlineItem(**{**vars(item), 'focused': item.name == name}) if item.role != 'text' else item
            for item in self.items
        ]

    async def outline(self) -> Outline | None:
        return Outline(title='Checkout', items=tuple(self.items)) if self.available else None


def tabs(url: str, title: str = 'Checkout') -> Tabs:
    return Tabs(tabs=(Tab(tab_id='t', url=url, title=title, active=True),))


async def type_into(lesson: Lesson, page: Page, name: str, text: str) -> None:
    """Focus a field (as a click would) and type into it a character at a time, as the live view's page sends."""
    page.focus(name)
    await lesson.before(MouseDown(at=Point(x=1, y=1)))
    await lesson.before(MouseUp(at=Point(x=1, y=1)))
    for character in text:
        await lesson.before(Type(text=character))


@pytest.mark.parametrize(
    'sensitive',
    [
        field('Password', secure=True),  # type=password, or autocomplete marks it (outline.js)
        field('Card number'),
        field('CVC'),
        field('Security code'),
        field('One-time code'),
        field('Enter the 6-digit verification code'),
        field('PIN'),
        field('Accept', role='button'),  # not a text field: typing there is not recorded either
    ],
)
async def test_nothing_typed_into_a_sensitive_field_is_recorded(sensitive: OutlineItem) -> None:
    page = Page(field('Email'), sensitive, field('Pay', role='button'))
    lesson = Lesson('Pay for my order', page.outline)
    await lesson.start(tabs('https://shop.example/checkout?session=abc123'))
    await type_into(lesson, page, 'Email', 'pat@example.com')
    await type_into(lesson, page, sensitive.name, SECRET)
    await lesson.before(Type(text=SECRET))  # pasted, all at once
    for character in SECRET:  # or sent as plain key presses
        await lesson.before(Press(key=character))
    await lesson.before(Press(key='Enter'))

    text = lesson.text()
    assert all(character not in text for character in ('Sw0rd', 'f1sh', 'hunter'))
    assert 'session=abc123' not in text  # a page's query can hold tokens
    assert 'Typed "pat@example.com" into textbox "Email"' in text
    assert text.count('Typed a secret') == 1
    assert all(step.text == '' for step in lesson.steps if step.kind == 'secret')


async def test_unknown_focus_counts_as_a_secret() -> None:
    page = Page(field('Notes'), available=False)  # an engine that cannot read the page
    lesson = Lesson('Leave a note', page.outline)
    await lesson.before(Type(text=SECRET))
    assert SECRET not in lesson.text() and 'Typed a secret' in lesson.text()

    nothing_focused = Page(field('Notes'))
    lesson = Lesson('Leave a note', nothing_focused.outline)
    await lesson.before(Type(text=SECRET))
    assert SECRET not in lesson.text()


async def test_a_page_summary_names_controls_but_never_their_values() -> None:
    filled = OutlineItem(role='textbox', name='Email', x=0, y=0, width=0, height=0, value='pat@example.com')
    password = OutlineItem(role='textbox', name='Password', x=0, y=0, width=0, height=0, value='7 characters')
    heading = OutlineItem(role='heading', name='Sign in', x=0, y=0, width=0, height=0, level=1)
    page = Page(heading, filled, password, field('Sign in', role='button'))
    lesson = Lesson('Sign in', page.outline)
    await lesson.start(tabs('https://shop.example/login', 'Sign in'))
    words = lesson.steps[0].words()
    assert words.startswith('Started on page "Sign in" at https://shop.example/login')
    assert 'headings: Sign in' in words and 'textbox "Email"' in words and 'button "Sign in"' in words
    assert 'pat@example.com' not in words and '7 characters' not in words


async def test_steps_say_what_was_done_where() -> None:
    orders = OutlineItem(role='link', name='Your orders', x=10, y=10, width=100, height=20)
    text = OutlineItem(role='text', name='Welcome back', x=0, y=0, width=800, height=200)
    page = Page(text, orders, field('Search', role='searchbox'))
    lesson = Lesson('Find my orders', page.outline)
    assert not lesson.did_something
    await lesson.before(Click(target=Point(x=20, y=15)))  # the link, not the text around it
    await lesson.before(MouseDown(at=Point(x=500, y=150)))  # only text there
    for _ in range(3):
        await lesson.before(Press(key='Tab'))
    await lesson.before(Press(key='a', modifiers=('Control',)))
    page.focus('Search')
    await lesson.before(Press(key='Enter'))
    await lesson.before(Scroll(delta_x=0, delta_y=300))
    await lesson.before(Scroll(delta_x=0, delta_y=300))
    await lesson.before(Navigate(url='https://shop.example/orders?token=s3cr3t#top'))
    await lesson.saw(tabs('https://shop.example/orders', 'Your orders'))
    await lesson.saw(tabs('https://shop.example/orders', 'Your orders'))  # the same page again: no new step
    assert lesson.did_something
    assert [step.words() for step in lesson.steps] == [
        'Clicked link "Your orders"',
        'Clicked text "Welcome back"',
        'Pressed Tab 3 times',
        'Pressed Control+a',
        'Pressed Enter on searchbox "Search"',
        'Scrolled down 2 times',
        'Typed the address https://shop.example/orders into the address bar',
        (
            'Reached page "Your orders" at https://shop.example/orders; headings: Welcome back; controls: link "Your '
            'orders", searchbox "Search"'
        ),
    ]
    assert lesson.text().startswith('Goal: Find my orders\n\nWhat the user did, in order:\n1. Clicked link')


async def test_the_page_moving_the_focus_by_itself_is_followed() -> None:
    """A page moves the focus with no input from the user (to the password once the username is in): the next
    keystroke goes where the focus is now, so the password is still a secret."""
    page = Page(field('Username'), field('Password', secure=True))
    lesson = Lesson('Sign in', page.outline)
    await type_into(lesson, page, 'Username', 'alice')
    page.focus('Password')
    for character in SECRET:
        await lesson.before(Type(text=character))
    assert [step.words() for step in lesson.steps] == [
        'Clicked the page',
        'Typed "alice" into textbox "Username"',
        'Typed a secret into textbox "Password" (not recorded)',
    ]
    assert is_secret(None) and not is_secret(field('Email'))


# --- over the WebSocket ---


async def until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        await asyncio.sleep(0.02)


class RecordingTeacher:
    def __init__(self) -> None:
        self.lessons: list[tuple[UserId, str, str]] = []

    async def draft(self, *, user_id: UserId, goal: str, lesson: str) -> Drafted:
        if goal == 'fail':
            raise TeachingFailed('Sammy could not write a draft from that just now. Please try again.')
        self.lessons.append((user_id, goal, lesson))
        return Drafted(skill_id='skill-1', name=goal)


async def test_a_lesson_over_the_live_view() -> None:
    login = 'http://shop.test/login?next=/account'
    browser = FakeBrowser(
        pages={
            login: FakePage(
                title='Sign in',
                text='Sign in',
                elements=[
                    FakeElement(selector='#user', role='textbox', name='Username'),
                    FakeElement(selector='#password', role='textbox', name='Password', secret=True),
                    FakeElement(selector='#submit', role='button', name='Sign in'),
                ],
            )
        }
    )
    service = StubBrowserService(lambda: browser)
    await service.start(run_id='run', user_id='alice')
    await service.act(run_id='run', user_id='alice', action=Navigate(url=login))
    handoff = await service.start_handoff(run_id='run', user_id='alice', reason='Show me')
    handoffs = InMemoryHandoffs()
    handoffs.add(handoff)
    auth = StubAuthenticator()
    teacher = RecordingTeacher()
    app = live_view_app(service=service, handoffs=handoffs, auth=auth, teacher=teacher)
    try:
        async with serve_app(app) as base:
            url = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
            async with LiveViewClient.connect(url, session=auth.sign_in('alice')) as live:
                await live.wait_for_url(lambda url: url.startswith('http://shop.test/login'))
                assert live.hello is not None and live.hello.teach
                await live.teach('Sign in to the shop')
                username, password = browser.find('#user'), browser.find('#password')
                assert username is not None and password is not None
                browser.focused = username  # as a click on the field would
                await live.send(Type(text='alice'))
                await until(lambda: username.value == 'alice')
                browser.focused = password
                for character in SECRET:
                    await live.send(Type(text=character))
                await until(lambda: password.value == SECRET)  # the browser gets it all the same
                taught = await live.stop_teaching()
                assert taught.name == 'Sign in to the shop'

                # Stopping with nothing done, or a draft that fails, says so; the user can teach again.
                await live.teach('Do nothing')
                await live.send(TeachStop())
                await live.wait_until(lambda: len(live.errors) == 1)
                await live.teach('fail')
                await live.send(Press(key='Tab'))
                await live.send(TeachStop())
                await live.wait_until(lambda: len(live.errors) == 2)
                errors = live.errors
    finally:
        await service.close_all()
    ((user, goal, lesson),) = teacher.lessons
    assert (user, goal) == ('alice', 'Sign in to the shop')
    assert 'Started on page "Sign in" at http://shop.test/login;' in lesson
    assert 'Typed "alice" into textbox "Username"' in lesson
    assert 'Typed a secret into textbox "Password"' in lesson
    assert SECRET not in lesson and 'next=' not in lesson
    assert errors == [
        'Nothing was recorded. Press Teach Sammy, then do the task.',
        'Sammy could not write a draft from that just now. Please try again.',
    ]


async def test_no_teacher_no_lessons() -> None:
    browser = FakeBrowser()
    service = StubBrowserService(lambda: browser)
    await service.start(run_id='run', user_id='alice')
    handoff = await service.start_handoff(run_id='run', user_id='alice', reason='Sign in')
    handoffs = InMemoryHandoffs()
    handoffs.add(handoff)
    auth = StubAuthenticator()
    try:
        async with serve_app(live_view_app(service=service, handoffs=handoffs, auth=auth)) as base:
            url = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
            async with LiveViewClient.connect(url, session=auth.sign_in('alice')) as live:
                await live.wait_until(lambda: live.hello is not None)
                assert live.hello is not None and not live.hello.teach
                await live.send(TeachRequest(goal='Anything'))
                await live.wait_until(lambda: bool(live.errors))
                assert live.errors == ['Teaching Sammy is not available here.']
    finally:
        await service.close_all()
