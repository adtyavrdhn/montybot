# Hand-off proof of concept

An agent drives a remote headless browser with tool calls. When it gets stuck, the browser session moves over a
socket to the user's machine and opens in a sandboxed local window. The user does the blocked step and clicks
**Return control to agent**. The session moves back, and the agent carries on.

```
remote (Sammy)                                   user's machine
  agent ── navigate / click / read_page ──> headless Chromium or Servo
  agent ── ask_user("please sign in")
     export cookies + storage, close the context  ── handoff ──>  sandboxed Chromium window,
                                                                    seeded with that state
                                                                    user signs in, adds coffee
     open a new context from the returned state   <── returned ──  "Return control" (or close the window)
  agent ── click #add-eggs, #add-milk, #add-bread ──> cart: coffee, eggs, milk, bread
```

The remote browser is headless Chromium (Playwright) or Servo (WebDriver), picked with `--engine`. The user's window
is always Chromium. The state format (`state.py`) is engine-neutral: cookies in WebDriver's shape plus storage maps
per origin, so a session moves between engines, as in Servo, then Chromium, then Servo again.

## Run it

```bash
cd poc
uv sync

# terminal 1: the Sammy side (the demo shop, a scripted agent, the hand-off socket)
uv run python -m sammy_poc.remote

# terminal 2: the user's machine; paste the command terminal 1 prints
uv run python -m sammy_poc.local --token TOKEN
```

A Chromium window opens on the shop's sign-in wall, with a bar at the bottom. Sign in with any username and the
password `hunter2`, optionally add an item, then click **Return control to agent**. Terminal 1 shows the agent
finishing with your login and your item in the cart.

- `--engine servo` on the remote side uses Servo instead of Chromium. It expects `servoshell` at
  `~/.cache/sammy/servo/Servo.app/Contents/MacOS/servoshell`; pass `--servo-binary` to change that. The Homebrew
  cask is disabled because it fails Gatekeeper, so take the build from the
  [Servo release page](https://github.com/servo/servo/releases) and check its `.sha256`.
- `--auto-demo` on the local side plays the user headlessly. That is how this was tested.
- `--model anthropic:claude-sonnet-4-5` (or any Pydantic AI model string) on the remote side swaps the scripted agent
  for a real model, using the same tools. That needs an API key and was not tested.

## What it shows (tested 2026-10-06, macOS, Playwright 1.63, Servo 0.7.0, pydantic-ai-slim 2.54)

Both engines finish with the same cart (coffee, eggs, milk, bread) and the same view count (3).

- **HttpOnly cookies travel.** The demo shop's `sid` login cookie is HttpOnly, so page JavaScript cannot copy it.
  Chromium's own cookie export carries it both ways. Servo takes it in correctly, but cannot give it back (see
  below).
- **localStorage travels.** The cart lives in localStorage. The item the user added locally is in the agent's final
  cart.
- **sessionStorage travels for the handed-over tab.** It is seeded with an init script before any page script runs.
  The shop's per-tab view counter keeps counting across machines (3 at the end: 1 local, 2 remote).
- **One writer at a time.** The remote side exports the state and closes its browser context before sending it, and
  opens a fresh context only from what comes back.
- **The local window is sandboxed.** It is a separate Chromium with a throwaway profile, never the user's own
  browser profile. Chromium's sandbox is on (checked: no `--no-sandbox` flag), and downloads are off.
- **Closing the window or quitting the browser also returns control.** After a quit, the state comes from the last
  page load, so changes made after that load are lost.
- **The hand-off socket checks a token.** A wrong token is rejected.

## Servo (`--engine servo`)

Servo 0.7.0, driven over its built-in W3C WebDriver (`servoshell --headless --webdriver=PORT`). *Measured* on this
Mac:

| Works | Does not work |
|---|---|
| Starting a process and a session: about 0.2 s | **Get All Cookies leaves out HttpOnly cookies**: `[]` while signed in |
| Navigate, find elements, type, click, Execute Script | More than one WebDriver session per process ("Session is already started") |
| Add Cookie, including `httpOnly: true`: the site sees it and `document.cookie` does not | SIGTERM: Servo ignores it, so the backend kills it |
| Reading and writing localStorage and sessionStorage with Execute Script | Saving cookies to `--config-dir`: only storage is written there |
| `navigator.webdriver` is undefined, unlike Chromium's `true` | |

What that means for hand-off:

- **Servo can take in a login, but not give one back.** It works in this demo because the user signs in on their
  own machine, in Chromium. A login the agent makes itself in Servo cannot be handed to the user or saved to the
  cookie jar. Neither can a session cookie the site refreshes while Servo holds it. On export, the backend sends back
  the HttpOnly cookies it was seeded with, which may be out of date by then.
- **Servo would not work as the user's local window.** The user's sign-in there would be invisible to the export.
- **Each run gets its own Servo process** and a throwaway config folder. That fits one browser per run anyway.
- **Seeding takes extra page loads.** WebDriver only sets cookies and storage for the page currently open, so the
  backend opens each origin first, then the hand-off URL, then reloads it once sessionStorage is in place.
- **Its user agent says Firefox 153 with a Servo suffix.** A session that moves between Servo and the user's Chromium
  changes browser fingerprint each time, which risk-scoring sites may notice.

Fixing HttpOnly export would need a change in Servo's WebDriver (the spec includes HttpOnly cookies in Get All
Cookies), or a cookie-export API on Servo's embedding (Rust) side.

## What it does not carry

- IndexedDB, Cache Storage, service workers, and HTTP cache.
- In-page memory, such as a form filled in but not submitted, scroll position, and other tabs' sessionStorage.
- Passkeys and client certificates, which are bound to a device by design.

## What this proof of concept leaves open

- **Sites that bind a session to a device or address.** This test runs both sides on one machine. Real retail
  sites score risk by IP address and browser fingerprint. A login made on a home connection and then used from a
  datacenter may be challenged again or revoked. This is the main thing to test against walmart.com.
- **The local window looks automated.** *Measured:* it reports `navigator.webdriver = true`, even with Playwright's
  `--enable-automation` flag removed, because Playwright drives it over the CDP pipe. A bot check could flag the user's
  own sign-in. A likely fix is to seed a throwaway profile headlessly, then start a plain Chrome subprocess on that
  profile with no debugging connection. The user closes the window to return control, and the state is read back by
  opening the profile headlessly again. sessionStorage and the Return bar would be lost that way. Not built yet.
- **Transport.** The state is a set of live credentials. A real deployment needs TLS, and a signed token that is
  short-lived and good for one hand-off only, instead of one shared token on localhost.
- **The local app.** A real client would be a small app that ships or finds a browser, not a Python script. With
  `channel="chrome"`, Playwright can use the user's installed Chrome with a fresh profile.
- **Page access to the bar.** Page scripts can call `window.sammyReturn()`. The worst they can do is end the
  hand-off early.
- **Stronger local isolation.** On Linux, the local window could run inside bwrap like the remote browser. On macOS,
  Chromium already sandboxes its renderers with Seatbelt, and wrapping all of Chrome in `sandbox-exec` is not
  supported.

## Files

| File | Holds |
|---|---|
| `sammy_poc/state.py` | `BrowserState`: URL, cookies, localStorage and sessionStorage, plus conversions to and from Playwright |
| `sammy_poc/browser.py` | Seeding a Playwright context with a state, and exporting one |
| `sammy_poc/wire.py` | Newline-delimited JSON over TCP, and the three messages |
| `sammy_poc/remote.py` | The Sammy side: the agent and its tools, the hand-off server, and the Chromium backend |
| `sammy_poc/servo.py` | The Servo backend: a WebDriver client, one Servo process per opened state |
| `sammy_poc/local.py` | The user's side: the sandboxed window, the Return bar, and sending the state back |
| `sammy_poc/demo_site.py` | A tiny shop with an HttpOnly login cookie, a localStorage cart and a sessionStorage counter |
