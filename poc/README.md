# Hand-off proof of concept

An agent drives a remote headless browser with tool calls. When it gets stuck, the browser session moves over a
socket to the user's machine and opens in a sandboxed local window. The user does the blocked step and clicks
**Return control to agent**. The session moves back, and the agent carries on.

```
remote (monty-bot)                                   user's machine
  agent ── navigate / click / read_page ──> headless Chromium
  agent ── ask_user("please sign in")
     export cookies + storage, close the context  ── handoff ──>  sandboxed Chromium window,
                                                                    seeded with that state
                                                                    user signs in, adds coffee
     open a new context from the returned state   <── returned ──  "Return control" (or close the window)
  agent ── click #add-eggs, #add-milk, #add-bread ──> cart: coffee, eggs, milk, bread
```

Servo is not installed here, so both sides use Playwright's Chromium. The state format (`state.py`) is
engine-neutral: cookies in WebDriver's shape plus storage maps per origin. A Servo backend driven over WebDriver
would fill and apply the same object.

## Run it

```bash
cd poc
uv sync

# terminal 1: the monty-bot side (the demo shop, a scripted agent, the hand-off socket)
uv run python -m montybot_poc.remote

# terminal 2: the user's machine; paste the command terminal 1 prints
uv run python -m montybot_poc.local --token TOKEN
```

A Chromium window opens on the shop's sign-in wall, with a bar at the bottom. Sign in with any username and the
password `hunter2`, optionally add an item, then click **Return control to agent**. Terminal 1 shows the agent
finishing with your login and your item in the cart.

- `--auto-demo` on the local side plays the user headlessly. That is how this was tested.
- `--model anthropic:claude-sonnet-4-5` (or any Pydantic AI model string) on the remote side swaps the scripted agent
  for a real model, using the same tools. That needs an API key and was not tested.

## What it shows (tested 2026-10-06, macOS, Playwright 1.63, pydantic-ai-slim 2.54)

- **HttpOnly cookies travel.** The demo shop's `sid` login cookie is HttpOnly, so page JavaScript cannot copy it;
  the browser engine's own cookie export carries it both ways.
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

## What it does not carry

- IndexedDB, Cache Storage, service workers, and HTTP cache.
- In-page memory, such as a form filled in but not submitted, scroll position, and other tabs' sessionStorage.
- Passkeys and client certificates, which are bound to a device by design.

## What this proof of concept leaves open

- **Sites that bind a session to a device or address.** This test runs both sides on one machine. Real retail
  sites score risk by IP address and browser fingerprint. A login made on a home connection and then used from a
  datacenter may be challenged again or revoked. This is the main thing to test against walmart.com.
- **Transport.** The state is a set of live credentials. A real deployment needs TLS, and a signed token that is
  short-lived and good for one hand-off only, instead of one shared token on localhost.
- **Servo.** WebDriver's Add Cookie only works for the current document's domain. A Servo backend would open each
  origin, add its cookies, write storage with Execute Script, and then load the hand-off URL. We have not checked
  whether Servo's WebDriver returns HttpOnly cookies and supports all of this.
- **The local app.** A real client would be a small app that ships or finds a browser, not a Python script. With
  `channel="chrome"`, Playwright can use the user's installed Chrome with a fresh profile.
- **Page access to the bar.** Page scripts can call `window.montybotReturn()`. The worst they can do is end the
  hand-off early.
- **Stronger local isolation.** On Linux, the local window could run inside bwrap like the remote browser. On macOS,
  Chromium already sandboxes its renderers with Seatbelt, and wrapping all of Chrome in `sandbox-exec` is not
  supported.

## Files

| File | Holds |
|---|---|
| `montybot_poc/state.py` | `BrowserState`: URL, cookies, localStorage and sessionStorage, plus conversions to and from Playwright |
| `montybot_poc/browser.py` | Seeding a Playwright context with a state, and exporting one |
| `montybot_poc/wire.py` | Newline-delimited JSON over TCP, and the three messages |
| `montybot_poc/remote.py` | The monty-bot side: the headless browser, the agent and its tools, and the hand-off server |
| `montybot_poc/local.py` | The user's side: the sandboxed window, the Return bar, and sending the state back |
| `montybot_poc/demo_site.py` | A tiny shop with an HttpOnly login cookie, a localStorage cart and a sessionStorage counter |
