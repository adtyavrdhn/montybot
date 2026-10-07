# The live view

When the bot asks for help, the user opens a link, sees the run's browser, drives it, and gives it back. Issue #14.

```
user's browser                     live view app (this package)              browser service (#10)
  page.html + live.js  <── WS ──>  /handoff/{id}/ws   ── live_view(id) ──>  FrameSource of the run's browser
     frames (JPEG/PNG)  <────────  pump               <── updates() ─────    Chromium: CDP screencast
     input (JSON)       ────────>  read               ── send(action) ───>   Servo: WebDriver screenshots
     "Give back"        ────────>  end_handoff(id)    ── Handoffs.given_back(GiveBack) ──> the run resumes
```

| Module | Holds |
|---|---|
| `app.py` | `live_view_app(service, handoffs, auth)`: the page, the WebSocket, one connection per hand-off |
| `wire.py` | The WebSocket messages, both ways |
| `chromium.py` | `CdpFrameSource`: `Page.startScreencast` for frames, `Input.dispatch*Event` for input, popups followed |
| `webdriver.py` | `WebDriverFrameSource`: polled Take Screenshot for frames, Perform Actions for input, windows followed |
| `polling.py` | `PollingFrameSource`: any `BrowserBackend`, from `screenshot()` and `act()` |
| `auth.py` | `Authenticator`, and `StubAuthenticator` standing in for the web app's sign-in (#8) |
| `handoffs.py` | `Handoffs` (find a hand-off, report the give-back), and `InMemoryHandoffs` standing in for #4 and the run |
| `activity.py` | The give-back summary: counts of what the user did, never what they typed |
| `client.py`, `scripted_user.py` | A WebSocket client, and the scripted user that signs in to the poc demo shop or holds a check |
| `conformance.py` | `HandoffRulesConformance`: the hand-off rules any `BrowserService` must keep, `live_view` included |

## Rules

- **The link names the hand-off; being signed in grants access.** The page and the WebSocket answer only the run's
  requester. Anyone else, or an unknown id, gets the same "not found" (`4404`). Not signed in is `4401`.
- **One connection per hand-off.** A new one (a reload, the link opened on a phone) takes over; the old one is closed
  with `4409` and its page offers "Use here". Reconnecting is always allowed while the hand-off is active. When it is
  over the socket closes with `4410`.
- **Cross-site pages cannot connect.** A browser always sends `Origin` on a WebSocket upgrade; it must be the app's
  own host or one of `allowed_origins`, otherwise the upgrade gets 403.
- **No screenshots for the model while the user drives.** The app reaches the browser only through
  `BrowserService.live_view` and `end_handoff`, with the hand-off id. The service refuses `screenshot`, `snapshot`
  and `act` without that id (`HandoffActive`), so nothing of the page reaches the agent or its history. Frames go to
  the socket only: never logged or stored. On give-back the run gets a `GiveBack`: `HandoffEnded` plus a summary in
  words.
- **A dropped connection lets go.** Closing a source releases any held mouse button, so a check is never left held.
- **Phones get a phone layout.** The page reports the room it has for the picture (`viewport`) when it connects and
  when it resizes. Narrower than 900 CSS pixels (`PHONE_WIDTH`), the bot's browser is laid out at that size as a phone
  browser would (Chromium: `Emulation.setDeviceMetricsOverride` with `mobile`, at most 2000 pixels tall),
  so text is readable and fields can be tapped. Wider, it keeps its own desktop size, scaled down to fit. Closing the
  source (give-back, a dropped or replaced connection, the hand-off ending) restores the browser's own size, so the
  agent never sees the phone layout. Engines that cannot (Servo, polled) raise `NotSupported` and the app ignores it.
- **Popups and new tabs** become the active tab, as in a browser window; the page shows a tab picker when there is
  more than one. When a popup closes, and when the hand-off ends, the run's own tab is active again. Tabs the user left
  open stay open.

## The WebSocket

Text messages are JSON with a `kind`; input uses the contract's action names. Frames are binary: a 4-byte length, a
JSON header (`seq`, `width`, `height` in CSS pixels, `mime`), then the image. See `wire.py`.

```
page -> server   mouse_down {x, y, button}  mouse_move {x, y}  mouse_up {x, y, button}  click {x, y}
                 type {text}  press {key, modifiers}  scroll {delta_x, delta_y, x?, y?}  switch_tab {tab_id}
                 viewport {width, height}  give_back
server -> page   hello {handoff_id, reason}  tabs {tabs}  error {message}  ended {given_back}  + binary frames
```

The web app (#8) can embed `/handoff/{id}` in an iframe (set `frame_ancestors`) or talk to the WebSocket itself.

## Engines

| | Chromium | Servo 0.7.0 |
|---|---|---|
| Frames | CDP screencast, JPEG, only when the page repaints | Take Screenshot polled up to 30 times a second, PNG, only changed frames sent |
| Input | `Input.dispatchMouseEvent` and `Input.dispatchKeyEvent` | Perform Actions, one event per call, each followed by Get Current URL (without that, an event sent as a navigation starts can hang the session for good) |
| Tabs | Playwright's `page` events | Get Window Handles, twice a second and after each input |
| Known gap | | Enter in a text field does not submit its form; Tab to the button and press Enter instead |

Measured on localhost (Apple Silicon Mac shared with other test runs, headless, 1280x720, through the app and a
WebSocket client; ranges over 5 or 6 runs of 40 presses each):

| | Chromium | Servo |
|---|---|---|
| Frames per second, page animating every frame | 22 to 48 | 16 |
| Frames per second, still page | 0 | 0 |
| Median frame | 8 to 9 KiB JPEG | 24 KiB PNG |
| Mouse down or up to the frame showing it, median | 6 to 8 ms | 69 to 96 ms |
| Same, 90th percentile | 11 to 59 ms | 99 to 137 ms |

`MONTYBOT_MEASURE=1 uv run pytest tests/liveview/test_liveview_measure.py -s` measures again.

## On the server

Not run on Linux yet. The plan, for #7 and #8:

- Mount the app in the web process behind TLS. The proxy must pass WebSocket upgrades (`Upgrade` and `Connection`
  headers) and allow idle sockets for the length of a hand-off; a still page sends no frames.
- The browser service and the app may be separate processes; `LiveViewService` is the only call between them, so
  frames cross that wire once.
- Visible Chromium on Xvfb (#11) screencasts the same way as headless; nothing here depends on the display.

## Tests

`tests/liveview/`: the wire and the summary; the app over a real WebSocket with `FakeBrowser` (sign-in, one
connection, origins, reconnecting, give-back, the agent refused while the user drives, and the page in headless
Chromium); the hand-off rules against the stand-in service; and, on both engines, input, popups, letting go, U2 (sign
in to the poc shop through the live view) and U6 (press and hold).
