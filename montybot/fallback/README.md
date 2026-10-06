# Fallbacks

Code the agent does not use yet, kept ready in case a user path needs it. Nothing else under `montybot/` imports this
package, and `tests/fallback/test_not_imported.py` fails if something does.

## E2B Desktop (#22)

`e2b_desktop.py` runs Google Chrome on an [E2B Desktop](https://e2b.dev/docs/use-cases/computer-use) sandbox
(Ubuntu 22.04, Xvfb, Xfce, noVNC) as a `BrowserBackend`, so a session can move between our browser and a full
desktop with `BrowserState`.

```python
from montybot.browser.contract import Click, Navigate, Selector, Type
from montybot.fallback.e2b_desktop import E2BDesktopBrowser

browser = E2BDesktopBrowser()            # reads E2B_API_KEY
await browser.open(state)                # starts a sandbox and Chrome, seeded with the state
await browser.act(Navigate(url='https://shop.example/login'))
await browser.act(Type(text='mike', target=Selector(css='#user')))
link = await browser.stream_url()        # noVNC for a human; the link holds a password
await browser.pause()                    # not billed while paused; memory and processes are kept
await browser.resume()
state = await browser.release()          # cookies (HttpOnly too) and storage out; kills the sandbox
```

| Part | How |
|---|---|
| Clicks, typing, keys, press-and-hold | The `e2b-desktop` SDK, which runs xdotool on the VM's display |
| Finding an element | `cdp.py` scrolls it into view and returns its centre plus the viewport's place on the screen |
| Loading pages, page text, scrolling, viewport screenshots | CDP |
| Cookies in and out, HttpOnly included | CDP `Storage.setCookies` and `Storage.getCookies` |
| localStorage and sessionStorage in | CDP: an empty page is served on each origin by intercepting the request, and the items are set there |
| localStorage out for other origins | The same trick in a short-lived second tab, so the user's tab is left alone |
| Whole-screen screenshot | The SDK (`scrot`) |
| Live view for a human | The SDK's noVNC stream, started with a password |
| Pause and resume | The SDK's `pause()` and `connect()` on the same object, which keeps the stream state |

### Why CDP inside the VM

Chrome only gives HttpOnly cookies to the browser itself or to a debugging connection. Chrome runs with
`--remote-debugging-port=0`, which binds a free port on the VM's loopback and writes it into the profile. `cdp.py`
(stdlib only, Python 3.10) is uploaded once and run with the VM's python3 for each step, through E2B's command API.
The port is never published through E2B's public hostnames. Requests that hold cookies or storage go in a file the
helper deletes after reading, not on the command line.

### Limits

- Refs are not supported (`NotSupported('ref')`) until #13 defines the snapshot format.
- One tab: `cdp.py` drives Chrome's first tab. Tabs a human opens over noVNC are not followed.
- Each step is one E2B command round trip, and a click or key press waits 0.3 s for any navigation to start.
- `Press` with modifiers works for letters, digits, named keys and common punctuation; other characters raise
  `ActionFailed`. A lone character is typed as text.
- Cookies with `SameSite=None` must also be `Secure`, or Chrome refuses them.
- E2B has no static egress IP; traffic leaves from Google Cloud ranges.

### Cost

E2B bills per second while a sandbox runs: $0.000014 per vCPU and $0.0000045 per GiB of memory (e2b.dev/pricing,
2026-10-06), and a paused sandbox is not billed for compute. The public `desktop` template is built with 8 vCPUs and
8 GiB, which is about **$0.53 an hour**. A 2 vCPU, 4 GiB sandbox would be about $0.17 an hour. The Pro plan adds $150 a
month. `cost_per_hour()` does the sum.

### Tests

- `tests/fallback/test_e2b_desktop.py` runs the contract's conformance suite and the desktop extras against a local
  stand-in desktop: Chrome for Testing, headless, on this machine, with CDP input events in place of xdotool. No
  network, no key.
- `tests/fallback/e2b_desktop_trial.py` is the real thing: it needs `E2B_API_KEY`, starts two desktops, and measures
  start, pause and resume times, whether the tab and cookies survive a pause, and whether a session moves to a second
  desktop. Without the key it prints a report with `"run": false` and exits 0.

```bash
E2B_API_KEY=... uv run python tests/fallback/e2b_desktop_trial.py --paused-for 30
```
