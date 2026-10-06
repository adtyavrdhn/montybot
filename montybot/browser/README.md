# The browser contract

What every browser engine implements, and how the agent and the web app reach a run's browser. Issue #1; the code is
the source of truth and this page is the map.

| Module | Holds | Built on it |
|---|---|---|
| `state.py` | `BrowserState`, `Cookie`: one tab's URL, cookies (HttpOnly included), localStorage and sessionStorage per origin | the sign-in jar (#4) |
| `contract.py` | `BrowserBackend`, the actions and targets, `Snapshot`, `Screenshot`, the errors | Chromium (#11), Servo (#12), snapshot (#13), E2B Desktop (#22) |
| `service.py` | `BrowserService`: the API the agent and the web app call, with run id and user id on every call | browser service (#10), live view (#14), Monty host functions (#5) |
| `fake.py` | `FakeBrowser`: an in-memory backend with scriptable pages | agent-side work and fixture tests (#2, #5) |
| `conformance.py` | `BrowserBackendConformance`: the tests every backend passes | every backend |
| `chromium.py`, `chromium_linux.py` | `ChromiumBackend`: real Chrome through Playwright; Xvfb and bwrap on Linux. See [`CHROMIUM.md`](CHROMIUM.md) | #11 |

## `BrowserBackend`

One engine, one tab, one caller at a time. The browser service owns backends; nothing else holds one.

```
closed --open(state)--> open --release()--> closed      returns the state; the session moves to someone else
                        open --close()----> closed      discards the state
                        open --export()---> open        returns the state and keeps going (for saving mid-run)
```

- `open(state | None)` seeds cookies and localStorage before the first page load, and sessionStorage before
  `state.url`'s scripts run (or reloads once after seeding).
- `export()` returns every cookie for every domain, HttpOnly included. **An engine that cannot read HttpOnly cookies
  raises `NotSupported('export')`.** It must not return the cookies it was seeded with as if they were current.
- `act(action)` returns once the action is done and any navigation it started has loaded.
- `snapshot()` returns `url`, `title` and `text`; the text format and refs belong to #13. The text always includes
  the page's visible text, and masks typed passwords.
- `screenshot()` returns a PNG of the viewport, plus the viewport's size in CSS pixels (the space `Point` uses).
- `close()` is safe in any state. A closed backend can be opened again.

`export()` is one method more than the issue lists. The service needs it to save state without closing the browser,
for example at the end of a hand-off, while the user's page stays as it is.

### Actions

Frozen, keyword-only dataclasses. Each has a `kind` field, so a union of them decodes from JSON without guessing.

| Action | Does |
|---|---|
| `Navigate(url)` | Loads an absolute URL |
| `Click(target)` | Left click on an element (scrolled into view) or at a point |
| `Type(text, target=None)` | Key presses. With a target: focus it and replace its value. Without: type at the caret |
| `Press(key, modifiers=())` | One key, as a DOM `KeyboardEvent.key` (`Enter`, `ArrowDown`, `a`), with `Alt`, `Control`, `Meta`, `Shift` held |
| `Scroll(delta_x=0, delta_y=0, at=None)` | A mouse wheel turn in CSS pixels, at `at` or the viewport's centre |
| `MouseDown(at, button)`, `MouseMove(at)`, `MouseUp(at, button)` | Press-and-hold and drag, for the live view (#14) |

Targets: `Selector(css=...)`, `Ref(ref=...)` from the latest snapshot, or `Point(x=..., y=...)` in viewport CSS pixels.

### Errors

All subclass `BrowserError`, and their messages are safe to show the model (no cookie values, storage or typed text).

| Error | Means |
|---|---|
| `NotSupported(feature, engine=...)` | The engine cannot do this at all. `feature` is one of `Feature`: an action (`navigate`, `click`, `type`, `press`, `scroll`, `mouse`), a target kind (`selector`, `ref`, `point`), or `snapshot`, `screenshot`, `export`. `features_of(action)` lists what an action needs |
| `TargetNotFound(target)` | No element matched, after the backend's short wait |
| `ActionFailed` | The engine tried and failed: an unreachable site, a timeout |
| `LifecycleError` | Called in the wrong state, such as `act` before `open`: a bug in the caller |

Nothing is ever a silent no-op: an engine either does the action or raises.

## `BrowserService`

One long-lived process owns every browser, so a browser outlives the agent attempt that started it and survives a
pause. Every call takes keyword-only `run_id` and `user_id`. A run that is not the user's, does not exist, or was
closed answers `UnknownRun`, the same in each case. Monty never sees these ids; its host functions are bound to one
run.

| Call | Returns | Notes |
|---|---|---|
| `start(run_id, user_id)` | `Started(url, reused, restarted)` | Starts from the user's saved state. Idempotent per run |
| `act(run_id, user_id, action, handoff_id=None)` | `ActionResult(restarted)` | |
| `snapshot(run_id, user_id)` | `SnapshotResult(snapshot, restarted)` | Agent only, so refused during a hand-off |
| `screenshot(run_id, user_id, handoff_id=None)` | `ScreenshotResult(screenshot, restarted)` | |
| `start_handoff(run_id, user_id, reason)` | `Handoff(handoff_id, run_id, user_id, reason)` | Idempotent: returns the active one |
| `end_handoff(run_id, user_id, handoff_id)` | `HandoffEnded(handoff_id, url, saved)` | Saves state. A second call raises `HandoffNotActive` |
| `save_state(run_id, user_id)` | `None` | Saves without closing. Allowed during a hand-off |
| `close(run_id, user_id)` | `bool`: saved | Saves, closes, and ends any hand-off |

- **Hand-off is a lease.** While a hand-off is active, `act` and `screenshot` are accepted only with its `handoff_id`
  (the live view); without it they raise `HandoffActive`, and `snapshot` always does. So no screenshot reaches the
  model while the user drives. A `handoff_id` that is not the active one raises `HandoffNotActive`.
- **Restarts are reported.** If the browser was closed since the last call (idle reaper, crash, server restart), the
  service starts a new one from the saved state first, and that call's result carries `Restarted(reason, url)` for
  the agent to read.
- **Saving.** `saved` is False, and the jar left as it was, only when the engine raises `NotSupported('export')`.
- The wire (HTTP, socket, in-process) is #10's choice. Everything is plain dataclasses, so it encodes to JSON.

## `FakeBrowser`

```python
from montybot.browser.contract import Click, Selector
from montybot.browser.fake import FakeBrowser, FakeElement, FakePage
from montybot.browser.state import BrowserState

browser = FakeBrowser(pages={
    'http://shop.test/': FakePage(
        title='Shop',
        elements=[FakeElement(selector='#cart', role='link', name='Cart', href='/cart')],
    ),
    'http://shop.test/cart': FakePage(title='Cart', text='Your cart is empty'),
})
await browser.open(BrowserState(url='http://shop.test/'))
await browser.act(Click(target=Selector(css='#cart')))
```

A page's `on_load(browser)` and `on_action(browser, action)` hooks stand in for its server and scripts, and may change
`browser.page`, `browser.cookies` and the storage maps (`browser.cookies_for(url)` gives what a server would receive).
`snapshot()` numbers elements as refs, valid until the next page load. Selectors match `FakeElement.selector`
exactly; there is no CSS and no layout. Unknown URLs load a "Not found" page. `not_supported={...}` makes it act like
a weaker engine, and `browser.actions` records everything it did.

## Conformance tests

```python
class TestChromium(BrowserBackendConformance):
    not_supported = frozenset()  # features this backend raises NotSupported for; those tests check it does

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        async with async_playwright() as playwright:
            yield ChromiumBackend(playwright=playwright, options=ChromiumOptions(headless=True))
```

The suite serves a small site on `127.0.0.1` (and `localhost`, for a second origin and cookie host) and checks: an
empty open; the lifecycle errors; a state round trip with an HttpOnly cookie, a persistent `Strict` cookie, cookies on
two hosts, localStorage on two origins and sessionStorage; `export()` leaving the browser open; reopening from a
released state; that the server receives the HttpOnly cookie while page scripts do not and see the storage at load;
each action type, including a click on a missing element and press-and-hold; and screenshots. Pages write what they
saw into their text, which the tests read with `snapshot()`.

It runs against `FakeBrowser` and against a `FakeBrowser` with features switched off, in `tests/browser/`, and
against `ChromiumBackend`, headless and headed, in `tests/browser/test_chromium.py`. Refs are not covered; #13 adds
those tests.
