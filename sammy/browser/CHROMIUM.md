# The Chromium backend

`ChromiumBackend` (`chromium.py`) runs real Chrome, driven by Playwright, behind `BrowserBackend`. Issue #11; it grew
from `ChromiumBrowser` in `poc/sammy_poc/remote.py`. It passes every conformance test, headless and headed.

```python
from playwright.async_api import async_playwright

from sammy.browser.chromium import ChromiumBackend, ChromiumOptions

async with async_playwright() as playwright:  # one per process, shared by every backend
    browser = ChromiumBackend(playwright=playwright, options=ChromiumOptions.server())
    await browser.open(state)
```

## Where Chrome runs

| Where | Options | Chrome |
|---|---|---|
| A Mac or a Linux desktop | `ChromiumOptions()` | a normal window on your screen |
| The Linux server | `ChromiumOptions.server()` | a normal window on its own Xvfb screen, inside bwrap |
| CI | `ChromiumOptions(headless=True)` | Playwright's headless shell, no window |

`ChromiumOptions.for_this_machine()` picks `server()` on Linux without a `DISPLAY`, and the defaults elsewhere.
Chrome is headed by default because sites spot headless Chrome.

## What `open()` and `close()` do

Each `open()` starts a new Chrome; nothing is shared between browsers.

1. Make a temporary folder (`sammy-chrome-*`) for this browser: the profile, the launch script, the Xvfb files.
2. Server only: start Xvfb on a free display it picks itself (`-displayfd`), the size of the window, with no TCP and
   no abstract socket, and a random cookie only this browser gets.
3. Server only: write `chrome-in-bwrap`, the program Playwright runs instead of Chrome (see below).
4. Start Chrome with Playwright on that profile. Playwright talks to it over `--remote-debugging-pipe` (fds 3 and 4);
   there is no debugging port.
5. Put the state's cookies (HttpOnly too) and localStorage in place before any page loads, then load `state.url`.
   sessionStorage goes in with a script that runs before the page's own, for that first load only.

`close()` stops Chrome and Xvfb and deletes the folder. So nothing outlives a browser except what `export()` returns.

## The sandbox

`chromium_linux.write_bwrap_script` follows the sketch in `DESIGN.md`, with these changes:

- **The profile keeps its path inside.** Playwright passes `--user-data-dir=PROFILE` itself, so the profile is
  mounted at the same path, not at `/profile`.
- **An empty environment** (`--clearenv`), then only `HOME`, `PATH`, `LANG`, `DISPLAY` and `XAUTHORITY`. The browser
  service's secrets never reach Chrome.
- **Only this browser's screen.** `/tmp` is private, and only this display's X socket and cookie file are mounted.
- **Distro paths are looked up when the script is written:** `/lib`, `/lib64`, `/bin` become symlinks or read-only
  mounts as on the host; `/etc/ssl`, `/etc/resolv.conf` and the like are mounted if present.
- Chrome's own sandbox stays on (`chromium_sandbox=True`). It needs unprivileged user namespaces inside bwrap.

- **Its own network, with one way out.** `--unshare-net` leaves Chrome only a loopback interface. `socat` inside
  forwards `127.0.0.1:1080` to a Unix socket mounted from outside, where `EgressProxy` (`egress.py`) answers SOCKS5.
  On the deployed server the proxy runs in a container with no database network or app secrets; the app mounts its
  socket directory read-only. The **directory**, not the socket inode, is mounted in bwrap, so new connections use
  the replacement socket after a proxy restart. The app checks the SOCKS greeting before starting Chrome and fails
  closed if the sidecar is down. Local server tests can use an in-process per-browser proxy instead. Chrome starts
  with `--proxy-server=socks5://127.0.0.1:1080 --proxy-bypass-list=<-loopback>`, so
  every connection, loopback included, goes there with its host name; the proxy resolves it and refuses any private,
  loopback or link-local answer (`ipaddress.is_global`). Pages cannot reach the app's database, the host, or the
  cloud metadata address, by name or by number. Only TCP leaves, so there is no QUIC and no WebRTC UDP.
  `ChromiumOptions.allow_private_networks` lifts the address check, for fixture sites in tests.

## Behaviour worth knowing

- **`act` waits for the page an action opens.** After a click, key press, typed text or mouse release, it watches
  50 ms for a main-frame navigation, and if one starts, waits for the new page's `load`.
- **`snapshot()` and refs** come from #13's `SnapshotWalker` (see `README.md`): the same text and refs as Servo for
  the same page, typed passwords masked. A click on a ref is a mouse click at the element's centre.
- **Selectors are CSS only** (`css=` in Playwright), and an invalid one raises `TargetNotFound`.
- **No automation giveaways in the page:** `--enable-automation` is dropped (no "controlled by automated software"
  bar) and `navigator.webdriver` is false. A test checks both, headed.
- **A page that fails to load** raises `ActionFailed` with Chrome's `net::ERR_...` code. `export()` and `snapshot()`
  then report the URL that failed, as the address bar does, not Chrome's `chrome-error://` page.
- **Downloads are off.** Popups and new tabs are not followed: the backend stays on its one tab.

## Measured on a Mac

Apple Silicon Mac, macOS, Playwright 1.63 (Chrome for Testing 153), 2026-10-06. Five runs each, medians, from
`uv run python tests/browser/bench_chromium.py`. Start is `open()` with cookies and no page; load is the `Navigate`
after it. Memory is RSS summed over every process of the browser; CPU is CPU time summed over them.

| Mode | Page | Start | Load | Processes | RSS | CPU, start to 2 s after load | CPU when idle |
|---|---|---|---|---|---|---|---|
| headed | demo shop | 0.36 s | 0.03 s | 7 | 953 MiB | 1.16 s | 1.2 % |
| headed | example.com | 0.34 s | 0.07 s | 7 | 974 MiB | 1.15 s | 1.4 % |
| headless (shell) | demo shop | 0.07 s | 0.01 s | 4 | 273 MiB | 0.18 s | 0.4 % |
| headless (shell) | example.com | 0.07 s | 0.09 s | 4 | 300 MiB | 0.21 s | 0.4 % |
| headless (full Chrome) | demo shop | 0.30 s | 0.01 s | 7 | 934 MiB | 0.84 s | 2.0 % |
| headless (full Chrome) | example.com | 0.25 s | 0.08 s | 7 | 944 MiB | 0.87 s | 1.6 % |

- The demo shop is `poc/`'s, signed in with its HttpOnly cookie. Idle CPU is the 5 s after that, as a share of one
  core. Closing takes 0.02 to 0.1 s.
- **Summed RSS counts shared pages once per process.** macOS `footprint` for the same processes, once: about
  430 MB headed and 51 MB for the headless shell.
- **Headed costs the same as full Chrome headless.** The extra cost is Chrome itself, not the window: the headless
  shell is a smaller build.
- On the server, Xvfb and bwrap add their own processes, which the script counts. `--server` on the script measures
  that; it has not run yet.

## On Linux

`tests/browser/test_chromium_linux.py` runs the launch path on a Mac with stand-in `bwrap` and `Xvfb` scripts:
Playwright runs the generated script, the CDP pipe survives it, and the stand-ins check the command lines.
`test_real_bwrap_and_xvfb` and `test_real_bwrap_refuses_private_addresses` run the real thing, and skip unless bwrap,
Xvfb and socat are installed. They pass inside the app image (`Dockerfile`) with the container settings in
`deploy/compose.yaml`, on Ubuntu 24.04 arm64 (colima), as does `tests/test_deploy.py` against the whole deployed
stack. Not measured there yet: `bench_chromium.py --server`.
