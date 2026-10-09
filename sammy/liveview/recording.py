"""`Lesson`: what the user does in the live view while they teach Sammy a task ("Teach Sammy", #128), in words.

The user names the goal, does the task in Sammy's browser, and stops; a model turns the lesson into a draft skill
(`Teacher`, `sammy.teach`) that the user reviews before Sammy uses it.

Recorded: what they click or press Enter on (its role and name, from the page's outline), other keys such as Tab,
scrolls, the pages they reach (title, address without its query, headings and controls), the addresses they type into
the address bar, and what they type into ordinary fields.

Never recorded: what they type into a password field or another sensitive one (a card number, a security or one-time
code, anything the page marks for a password or card details). That is "typed a secret", with no text and no length.
When the field cannot be told (no outline, nothing focused, not a text field), typing counts as a secret too. Single
keys pressed without a modifier go the same way, so a password cannot slip through as key presses.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from sammy.browser.contract import Click, MouseDown, MouseMove, MouseUp, Navigate, Point, Press, Scroll, Type
from sammy.browser.live import LiveInput, Outline, OutlineItem, Tabs
from sammy.browser.service import UserId

MAX_STEPS = 300
"""A lesson keeps its first steps only: enough for any task worth a skill, and a bounded prompt."""
MAX_TYPED = 200
"""The most characters of one field's typing kept."""
PAGE_ITEMS = 12
"""Headings and controls a page's summary names at most, each."""

TEXT_ROLES = frozenset({'textbox', 'searchbox', 'combobox', 'spinbutton'})
SENSITIVE = re.compile(
    r'pass(word|code|phrase)?|\bpin\b|one[- ]?time|\botp\b|\b2fa\b|two[- ]factor|verif|security|\bcv[cv]2?\b|\bcsc\b'
    r'|card|expir|\bssn\b|social security|routing|account (number|no)|\biban\b|secret|token|api[- ]?key',
    re.IGNORECASE,
)
"""Field names that say the field holds a secret, whatever the page's markup says."""

StepKind = Literal['page', 'address', 'click', 'key', 'type', 'secret', 'scroll']


@dataclass(kw_only=True)
class Step:
    kind: StepKind
    target: str = ''
    """What it was done to, as `button "Buy again"`; empty when it cannot be told."""
    text: str = ''
    """Typed text (never for `secret`), a key, a direction, or a page's summary."""
    count: int = 1
    """How many times in a row: pressing Tab three times is one step."""

    def words(self) -> str:
        on = f' on {self.target}' if self.target else ''
        into = f' into {self.target}' if self.target else ''
        times = f' {self.count} times' if self.count > 1 else ''
        match self.kind:
            case 'page':
                return self.text
            case 'address':
                return f'Typed the address {self.text} into the address bar'
            case 'click':
                return f'Clicked {self.target or "the page"}'
            case 'key':
                return f'Pressed {self.text}{on}{times}'
            case 'type':
                return f'Typed "{self.text}"{into}'
            case 'secret':
                return f'Typed a secret{into} (not recorded)'
            case 'scroll':
                return f'Scrolled {self.text}{times}'


class TeachingFailed(Exception):
    """The lesson could not be made into a draft. The message is safe to show the user."""


@dataclass(frozen=True, kw_only=True)
class Drafted:
    skill_id: str
    name: str


class Teacher(Protocol):
    async def draft(self, *, user_id: UserId, goal: str, lesson: str) -> Drafted:
        """Turn the lesson (`Lesson.text()`) into a draft skill of the user's. Raises `TeachingFailed`."""
        ...


OutlineOf = Callable[[], Awaitable[Outline | None]]
"""What is on the active tab now, or None when the engine cannot tell."""


class Lesson:
    """Call `before` with each input before the browser gets it, and `saw` with each tab list."""

    def __init__(self, goal: str, outline: OutlineOf) -> None:
        self.goal = goal
        self.steps: list[Step] = []
        self._outline = outline
        self._url = ''

    @property
    def did_something(self) -> bool:
        return any(step.kind != 'page' for step in self.steps)

    async def start(self, tabs: Tabs | None) -> None:
        """The page the lesson starts on."""
        if tabs is not None:
            await self.saw(tabs)

    async def saw(self, tabs: Tabs) -> None:
        active = next((tab for tab in tabs.tabs if tab.active), None)
        if active is None or active.url == self._url:
            return
        self._url = active.url
        outline = await self._outline()
        first = not self.steps
        self._add(Step(kind='page', text=page_words(active.title, active.url, outline, first=first)))

    async def before(self, action: LiveInput) -> None:
        match action:
            case Type(text=text):
                await self._typed(text)
            case Press(key=key, modifiers=modifiers) if len(key) == 1 and set(modifiers) <= {'Shift'}:
                await self._typed(key)  # a character, however it was sent
            case Press(key=key, modifiers=modifiers):
                target = describe(await self._focused()) if key == 'Enter' else ''  # Enter activates what has focus
                self._repeat(Step(kind='key', target=target, text='+'.join([*modifiers, key])))
            case MouseDown(at=at) | Click(target=Point() as at):
                outline = await self._outline()
                self._add(Step(kind='click', target=describe(at_point(outline, at))))
            case Scroll(delta_y=delta_y, delta_x=delta_x):
                if abs(delta_y) >= abs(delta_x):
                    direction = 'down' if delta_y > 0 else 'up'
                else:
                    direction = 'right' if delta_x > 0 else 'left'
                self._repeat(Step(kind='scroll', text=direction))
            case Navigate(url=url):
                self._add(Step(kind='address', text=without_query(url)))
            case Click() | MouseMove() | MouseUp():
                pass  # a click happens on mouse down; the live view only clicks at points

    def text(self) -> str:
        """The lesson for the model that drafts the skill."""
        lines = [f'{number}. {step.words()}' for number, step in enumerate(self.steps, start=1)]
        return f'Goal: {self.goal}\n\nWhat the user did, in order:\n' + '\n'.join(lines)

    async def _typed(self, text: str) -> None:
        # The field is read for every keystroke: a page can move the focus by itself (to the password after the
        # username, along a one-time code's boxes), and a field remembered from before would then be the wrong one.
        field = await self._focused()
        target = describe(field)
        last = self.steps[-1] if self.steps else None
        if is_secret(field):
            if last is None or last.kind != 'secret' or last.target != target:
                self._add(Step(kind='secret', target=target))
        elif last is not None and last.kind == 'type' and last.target == target:
            last.text = (last.text + text)[:MAX_TYPED]
        else:
            self._add(Step(kind='type', target=target, text=text[:MAX_TYPED]))

    async def _focused(self) -> OutlineItem | None:
        outline = await self._outline()
        return next((item for item in outline.items if item.focused), None) if outline else None

    def _repeat(self, step: Step) -> None:
        last = self.steps[-1] if self.steps else None
        if last is not None and (last.kind, last.target, last.text) == (step.kind, step.target, step.text):
            last.count += 1
        else:
            self._add(step)

    def _add(self, step: Step) -> None:
        if len(self.steps) < MAX_STEPS:
            self.steps.append(step)


def is_secret(field: OutlineItem | None) -> bool:
    """Typing here is recorded as a secret: unknown, not a text field, a password, or named like a secret."""
    return field is None or field.secure or field.role not in TEXT_ROLES or SENSITIVE.search(field.name) is not None


def describe(item: OutlineItem | None) -> str:
    if item is None:
        return ''
    name = ' '.join(item.name.split())[:80]
    return f'{item.role} "{name}"' if name else item.role


def at_point(outline: Outline | None, at: Point) -> OutlineItem | None:
    """The smallest item whose box holds the point, a control before text."""
    if outline is None:
        return None
    hits = [
        item
        for item in outline.items
        if item.x <= at.x <= item.x + item.width and item.y <= at.y <= item.y + item.height and item.width > 0
    ]
    hits.sort(key=lambda item: (item.role in ('text', 'heading', 'image'), item.width * item.height))
    return hits[0] if hits else None


def without_query(url: str) -> str:
    """An address without its query and fragment, which can hold tokens and personal details."""
    parts = urlsplit(url)
    return parts._replace(query='', fragment='').geturl()


def page_words(title: str, url: str, outline: Outline | None, *, first: bool) -> str:
    """A page in a line: its title and address, then its headings and controls by name (never fields' values)."""
    where = f'page "{title}" at {without_query(url)}' if title else f'page {without_query(url)}'
    words = f'{"Started on" if first else "Reached"} {where}'
    if outline is None:
        return words
    headings = [' '.join(item.name.split())[:80] for item in outline.items if item.role == 'heading']
    controls = [describe(item) for item in outline.items if item.role not in ('heading', 'text', 'image')]
    if not headings:  # an engine without headings (the fake): the page's first line instead
        headings = [' '.join(item.name.split())[:80] for item in outline.items if item.role == 'text'][:1]
    for label, names in (('headings', headings), ('controls', controls)):
        if names:
            more = f', and {len(names) - PAGE_ITEMS} more' if len(names) > PAGE_ITEMS else ''
            words += f'; {label}: {", ".join(names[:PAGE_ITEMS])}{more}'
    return words
