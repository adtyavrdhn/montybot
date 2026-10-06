"""`Activity`: what the user did during a hand-off, in counts, for the agent's summary.

The agent resumes with words, not screenshots (DESIGN.md, "Return control"). The summary never holds what the user
typed or which keys they pressed, since that may be a password.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from montybot.browser.contract import Click, MouseDown, MouseMove, MouseUp, Press, Scroll, Type
from montybot.browser.live import LiveInput, Tabs


@dataclass(kw_only=True)
class Activity:
    started: float = field(default_factory=time.monotonic)
    clicks: int = 0
    keys: int = 0
    characters: int = 0
    scrolls: int = 0
    tabs_opened: int = 0
    _seen_tabs: set[str] = field(default_factory=set[str])

    def record(self, action: LiveInput) -> None:
        match action:
            case MouseDown() | Click():
                self.clicks += 1
            case Press():
                self.keys += 1
            case Type(text=text):
                self.characters += len(text)
            case Scroll():
                self.scrolls += 1
            case MouseMove() | MouseUp():
                pass

    def saw(self, tabs: Tabs) -> None:
        ids = {tab.tab_id for tab in tabs.tabs}
        if self._seen_tabs:
            self.tabs_opened += len(ids - self._seen_tabs)
        self._seen_tabs |= ids

    def summary(self, url: str) -> str:
        """Such as: "The user had the browser for 1 min 5 s. They clicked 3 times and typed 12 characters. They left
        it on https://shop.example/account." """
        seconds = round(time.monotonic() - self.started)
        held = f'{seconds // 60} min {seconds % 60} s' if seconds >= 60 else f'{seconds} s'
        did = [
            text
            for count, text in (
                (self.clicks, f'clicked {_times(self.clicks)}'),
                (self.keys, f'pressed {_plural(self.keys, "key")}'),
                (self.characters, f'typed {_plural(self.characters, "character")}'),
                (self.scrolls, f'scrolled {_times(self.scrolls)}'),
                (self.tabs_opened, f'opened {_plural(self.tabs_opened, "tab")}'),
            )
            if count
        ]
        actions = f'They {_join(did)}.' if did else 'They did not click or type anything.'
        return f'The user had the browser for {held}. {actions} They left it on {url}.'


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _times(count: int) -> str:
    return 'once' if count == 1 else f'{count} times'


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]
