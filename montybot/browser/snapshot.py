"""The agent's view of a page (#13): one JavaScript walker, `snapshot.js`, for every engine.

The walker reads the DOM and computed styles, never an accessibility tree, so Chromium and Servo print the same text
for the same page. A backend runs it through its own script execution (`RunScript`) and builds `Snapshot` and ref
actions on top with `SnapshotWalker`:

    walker = SnapshotWalker(run_script=lambda function, arg: page.evaluate(function, arg))  # Playwright
    snapshot = await walker.snapshot()
    lowered = await walker.resolve(Click(target=Ref(ref='12')))  # Click(target=Point(...)), in viewport pixels

The text format:

    # Sign in                           a heading, with one # per level
    Use your shop account.              visible text, one line per block
    [1] textbox "Username" value="mike" an element the agent can act on: [ref] role "name" then states
    [2] textbox "Password" type=password value="***"
    [3] checkbox "Remember me" checked
    [4] combobox "Sort" value="Price" options=["Name", "Price"]
    [5] link "Product: Eggs"
      [6] button "Add to cart"          inside the line above (a link or widget holding other controls)
    iframe "Newsletter"                 a same-origin frame; its lines follow, indented
      [7] button "Subscribe"
    iframe "Ads" (other origin, not shown)

Open shadow roots are walked in place of their host's children, with slotted content where the slot is; closed shadow
roots cannot be read by any script, so they are not shown. Typed passwords show as `***`. Text past the size budget
is cut at a line boundary and a last line says how many lines and refs were left out. If the first prose line alone
is too long, its prefix is shown with an explicit truncation notice; control lines are never partially shown.

Refs are numbers stamped on elements (the `data-montybot-ref` attribute, and a map in the page), and live as long as
the document. An element keeps its ref across snapshots for as long as it is on the page. When a re-render replaces
an element with one of the same role, name, id and frame, and no other element matches, the new one takes over the
ref. Anything else fails with `TargetNotFound` and says why: no snapshot yet, a new page has loaded, the element is
gone, or it is hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from typing import Protocol, cast

from montybot.browser.contract import (
    Action,
    ActionFailed,
    Click,
    Point,
    Ref,
    Snapshot,
    TargetNotFound,
    Type,
)

SNAPSHOT_JS: str = files('montybot.browser').joinpath('snapshot.js').read_text(encoding='utf-8')
"""The walker: a JavaScript function expression that takes one JSON argument and returns JSON."""

REF_ATTRIBUTE = 'data-montybot-ref'
"""The attribute the walker stamps on every element it gives a ref."""

DEFAULT_BUDGET = 12_000
"""The default size budget for `Snapshot.text`, in UTF-16 code units (as JavaScript counts them).
Match the agent's page budget so the walker cuts whole lines and retains its omitted-lines/refs notice."""

JSON = dict[str, 'JSON'] | list['JSON'] | str | int | float | bool | None


class RunScript(Protocol):
    """Call a JavaScript function expression with one JSON argument in the tab's top-level document, and return its
    JSON result. One line in each engine:

    - Playwright: `await page.evaluate(function, arg)`
    - WebDriver Execute Script: `{'script': webdriver_script(function), 'args': [arg]}`
    """

    async def __call__(self, function: str, arg: JSON, /) -> object: ...


def webdriver_script(function: str) -> str:
    """The body for WebDriver's Execute Script that calls `function` with the first argument."""
    return f'return ({function})(arguments[0]);'


_NOT_FOUND = {
    'no-snapshot': 'no snapshot of this page yet; refs come from snapshot()',
    'page-changed': 'a new page has loaded since the snapshot; take a new snapshot',
    'unknown': 'not a ref in the latest snapshot',
    'stale': 'the element is gone from the page (removed, or re-rendered as something else); take a new snapshot',
    'hidden': 'the element is hidden now; take a new snapshot',
}
_FAILED = {
    'disabled': 'is disabled',
    'offscreen': 'cannot be scrolled into the viewport',
    'covered': 'is covered by another element',
    'not-editable': 'cannot take typing',
    'not-focusable': 'cannot take focus',
    'readonly': 'is read-only',
    'no-option': 'has no option with that label or value',
}


@dataclass(kw_only=True)
class SnapshotWalker:
    """Snapshots and ref actions for one tab, through `run_script`. Keep one per backend tab, so refs from the latest
    snapshot are checked against the page they came from."""

    run_script: RunScript
    budget: int = DEFAULT_BUDGET
    """The most characters `Snapshot.text` may have."""
    _page: str | None = field(default=None, init=False, repr=False)
    """The id of the document the latest snapshot came from."""

    async def snapshot(self) -> Snapshot:
        """Walk the page, give its elements refs, and return the text."""
        result = _mapping(await self.run_script(SNAPSHOT_JS, {'op': 'snapshot', 'budget': self.budget}))
        self._page = str(result['page'])
        return Snapshot(url=str(result['url']), title=str(result['title']), text=str(result['text']))

    async def click_point(self, target: Ref) -> Point:
        """Scroll the ref's element into view and return the viewport point to click: its centre, checked to hit the
        element itself (or its label) rather than something on top of it."""
        result = await self._locate(target, 'click', '')
        return Point(x=float(cast(float, result['x'])), y=float(cast(float, result['y'])))

    async def focus_for_typing(self, target: Ref, text: str) -> bool:
        """Focus the ref's element and select its value, so typed keys replace it. Returns False when the script set
        the value itself and no keys are needed: a select's option, an empty `text`, or a date, number, colour or
        range input, whose widgets differ between engines."""
        result = await self._locate(target, 'type', text)
        return bool(result['keys'])

    async def resolve(self, action: Action) -> Action | None:
        """Turn an action on a ref into one the engine performs natively: a click at a `Point`, or typing at the
        caret. Returns None when the script already did it (see `focus_for_typing`). Other actions come back as they
        are."""
        match action:
            case Click(target=Ref() as target):
                return Click(target=await self.click_point(target))
            case Type(target=Ref() as target, text=text):
                return Type(text=text) if await self.focus_for_typing(target, text) else None
            case _:
                return action

    async def _locate(self, target: Ref, action: str, text: str) -> dict[str, object]:
        arg: JSON = {'op': 'locate', 'ref': target.ref, 'page': self._page, 'action': action, 'text': text}
        result = _mapping(await self.run_script(SNAPSHOT_JS, arg))
        error = result.get('error')
        if error is None:
            return result
        detail = str(result.get('detail') or '')
        if error in _NOT_FOUND:
            raise TargetNotFound(target, _NOT_FOUND[str(error)])
        reason = _FAILED.get(str(error), str(error))
        raise ActionFailed(f'ref {target.ref} {reason}' + (f': {detail}' if detail else ''))


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ActionFailed(f'snapshot.js returned {type(value).__name__}, not an object')
    return cast(dict[str, object], value)
