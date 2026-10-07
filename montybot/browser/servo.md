# The Servo backend

`ServoBackend` in `servo.py` runs Servo behind the browser contract (`contract.py`), over servoshell's built-in W3C
WebDriver. Issue #12. Tested with Servo 0.7.0 on macOS (Apple Silicon) and Ubuntu 24.04 (aarch64 locally, x86_64 in
CI's `servo-linux` job). The engine evaluation (#18) is at the end.

```python
from montybot.browser.servo import ServoBackend, ServoOptions

browser = ServoBackend(ServoOptions())  # Chrome user agent and site prefs on; export off on a stock build
await browser.open(state)
```

Get servoshell from the [Servo release page](https://github.com/servo/servo/releases) (check its `.sha256`). The
backend looks for it at `$MONTYBOT_SERVO_BINARY`, else `~/.cache/montybot/servo/Servo.app/Contents/MacOS/servoshell`
on macOS and `~/.cache/montybot/servo/servo/servoshell` on Linux.

## Conformance

All 16 tests of `BrowserBackendConformance` pass against a real servoshell 0.7.0, with
`not_supported = {'export'}`:

| Feature | Status | Fix it needs |
|---|---|---|
| `export`, `release` | `NotSupported` on a stock build: Get All Cookies hides HttpOnly cookies | Upstream: `patches/servo-webdriver-httponly.patch`. With a build that has it, `http_only_export=True` turns export on |
| everything else | Passes: open, seeding (HttpOnly too), storage, every action, refs (#13's `SnapshotWalker`), screenshots | |

## Decisions on the known gaps

1. **HttpOnly cookies missing from Get All Cookies.** Cause confirmed in the v0.7.0 source; the two-line fix is in
   `patches/` with a write-up. Until a patched build runs, `export()` raises `NotSupported('export')`, as the contract
   requires, so the service does not save a Servo session (`saved=False`). Servo can still take a sign-in in.
   `http_only_export=True` turns export on for a patched build. If an HttpOnly cookie seeded at `open` cannot be read
   back, the backend knows the build is not patched and keeps export off.
2. **Add Cookie only takes the current document's host.** Kept the open-the-bare-domain workaround, in a cheaper
   form: for each cookie, a side tab opens `https://HOST:1/` (the bare domain for a `.example.com` cookie). Port 1 is
   on the fetch spec's bad-port list, so Servo shows its error page at once: no DNS lookup, no request to the site,
   and no redirect to `www.` that would make Add Cookie refuse the domain. Tested in
   `test_cookies_are_seeded_from_the_bare_domain` with `shop.test` and `www.shop.test` in a hosts file.
   localStorage still needs a real document of its origin, so it is written on `ORIGIN/robots.txt`, a page without
   scripts. The user's tab only ever loads `state.url`.
3. **One session per process.** One Servo process per `open`, on a free port with a throwaway config folder. Measured
   settled memory below.
4. **SIGTERM and SIGINT ignored.** Confirmed: both left headless servoshell running after 3 s. `close()` sends SIGKILL
   to the process group (servoshell starts in its own session), waits, and deletes the config folder. Nothing is lost,
   because the profile is thrown away.

Found while building it:

- **Failed loads do not fail the WebDriver call.** Navigate to an unreachable site returns success and shows Servo's
  `neterror.html` ("Error loading page"). The backend spots that page and raises `ActionFailed`.
- **Domain cookies come back without the leading dot**, so they look host-only. On export the backend reads the
  cookie again from a made-up subdomain (`montybot-probe.HOST`, on the blocked port, so no request): only a domain
  cookie shows there. See `patches/README.md`.
- **The WebDriver server listens on `0.0.0.0`**, not loopback (`components/webdriver_server/lib.rs`). On this Mac
  each test's Servo is reachable from the LAN while it runs. In the server's jail Servo has its own network namespace,
  and the host reaches WebDriver only through a Unix socket in the profile folder (below). A one-line upstream change
  would fix it at the source.
- **Export reads only hosts the backend knows**: the seeded ones and every page the tab was on. A cookie set for a
  host the tab never showed, such as one set during a sign-in redirect chain, is missed. Servo's Rust embedding API
  (`SiteDataManager`) can list every cookie; WebDriver cannot.

## Site prefs and user agent

From the second spike (Aditya): with these prefs on and a Chrome user agent, Servo rendered example.com,
github.com/login and walmart.com, and walmart did not challenge it; a Firefox user agent was challenged every time.

- `SITE_PREFS`: `dom_intersection_observer_enabled`, `dom_resize_observer_enabled`, `dom_adoptedstylesheet_enabled`,
  `dom_crypto_subtle_enabled`, `dom_fontface_enabled`, `dom_indexeddb_enabled`, `layout_columns_enabled`,
  `layout_container_queries_enabled`, `layout_variable_fonts_enabled`. Names from `components/config/prefs.rs` and
  servoshell's `EXPERIMENTAL_PREFS` at v0.7.0. Off by default in 0.7.0: IntersectionObserver, adoptedStyleSheets,
  FontFace, IndexedDB, columns, container queries and variable fonts; ResizeObserver and `crypto.subtle` are on
  already, and are listed so a future default change does not drop them.
- `CHROME_USER_AGENT`: desktop Chrome for the platform. `navigator.userAgentData` and other Chrome-only APIs are still
  missing, so a careful fingerprinter can still tell.

`test_site_prefs_and_chrome_user_agent` checks both from a page.

## Measured (macOS 26.5, Apple M-series, 10 cores, Servo 0.7.0, default 1024x740 window)

| What | Result |
|---|---|
| `open(None)`: start the process and a session | median 0.11 s (0.08 to 0.19, 10 runs) |
| `open` with the conformance state (3 cookies on 2 hosts, storage on 2 origins, then the page) | 0.29 to 0.43 s |
| `export` of that state with `http_only_export` (2 hosts, 2 origins) | 0.17 s |
| Settled memory per process, 8 s after load: about:blank | RSS 100 MiB, footprint 80 MiB |
| fixture page | RSS 116 MiB, footprint 124 MiB; five at once: 105 to 108 MiB RSS each |
| example.com | RSS 91 MiB, footprint 127 MiB |
| github.com/login | RSS 227 MiB, footprint 228 MiB |
| Screenshot (WebDriver Take Screenshot, polled back to back) | 19 to 22 fps at 1024x740, about 45 ms each |
| by window size | 24 fps at 640x480, 23 at 1280x720, 18 at 1920x1080 |
| four processes polled at once | 21.5 to 23.5 fps each |

Footprint is macOS's `footprint` tool (`phys_footprint`). Linux numbers are still to measure.

Screenshots are about 45 ms whatever the page, so a live view (#14) gets about 20 fps by polling `screenshot()`. That
is enough to sign in and tap through a check. The spike reported 22 to 55 fps; its setup is not in the repo, so the
gap is not explained. A PNG of a plain page is 16 to 20 KiB; example.com was 153 KiB.

## Linux, in the server's jail

`ServoOptions(bwrap=True)`, or `BROWSER_BACKEND=montybot.engines:servo_server`, runs servoshell in Chromium's jail
(`chromium_linux.bwrap_command`): its own user, pid, IPC, UTS and network namespaces, host files read-only, only the
profile folder writable, `--die-with-parent`. Two socat processes inside are its only connections to the host:

- **Out:** Servo's HTTP proxy prefs (`network_http_proxy_uri`, `network_https_proxy_uri`) point at
  `127.0.0.1:1080`, which socat forwards to the `EgressProxy` (`egress.py`). Servo speaks only HTTP `CONNECT`
  (hyper-util's `Tunnel`), for `http://` too, so the proxy takes `CONNECT` as well as SOCKS5, with the same
  public-address check. Anything that skips the proxy, such as Servo's WebSockets (a direct `TcpStream` in
  `websocket_loader.rs`), finds no network: it fails rather than leaks.
- **In:** WebDriver's port is published only as `webdriver.sock` in the profile folder (mode 600), so nothing listens
  on the host's network.

Headless Servo renders in software, so it needs no Xvfb. On Ubuntu 24.04 servoshell needs `libgstreamer1.0-0`,
`libgstreamer-plugins-base1.0-0`, `libgstreamer-plugins-bad1.0-0`, `libegl1`, `libegl-mesa0`, `libgl1-mesa-dri`,
`fontconfig` and a font (`tests/linux/Dockerfile`). The app image does not ship Servo.

`tests/linux/run.sh` runs the tests in a Linux container (set `MONTYBOT_SERVO_BINARY` to an unpacked Linux release).
Podman, as on this Mac, mounts `/etc/hosts` and `/etc/resolv.conf` with flags an unprivileged bwrap cannot remount
read-only, so the jailed tests fail there for Chromium and Servo alike; CI's runners and the server's Docker do not.

## Engine evaluation (#18)

The end-to-end suite (`tests/e2e`, scripted model, fixture sites) with `--browser=servo`, on macOS:

| | Chromium (`e2e-chromium` in CI) | Servo 0.7.0 |
|---|---|---|
| Passed | all | 83 of 94 (81 before the hand-off fix below) |
| Sign-in hand-off | passes | fails: `autofocus` is ignored and Enter does not submit a form |
| Sign-in saved for the next run | passes | fails: no export of HttpOnly cookies (stock build) |
| Press-and-hold check in the live view | passes | fails when the browser is given back |
| Downloads (`test_files`) | passes | fails: `ServoBackend` has no downloads, and a ref went stale |
| Live view | CDP screencast | polled screenshots, about 20 fps |
| WebSockets on the server | through the proxy | none (fail closed) |

Found and fixed on the way: `hand_off` saved the browser before handing it over, and an engine that cannot export
made the whole hand-off fail. It now skips the save, as `approvals.save_browser` already did for other waits.

Servo 0.7.0 gaps that block it as the default, each needing an upstream fix or a workaround here:

1. **No session export** (HttpOnly cookies): sign-ins are not saved between runs. Fix: the patch in `patches/`, which
   means building Servo ourselves.
2. **`autofocus` ignored**, and **Enter does not submit forms** (implicit submission) for WebDriver key input. Agents
   and people both press Enter to search and sign in.
3. **No downloads** and **no WebSockets** behind the proxy.
4. **Key input with nothing focused** (typing, then Tab, then typing on a page body) made servoshell stop answering.

### Real sites from the server

On the GCP VM (datacenter address), both engines jailed with their own egress proxy, a fresh browser per site, the page
read 5 seconds after it loaded:

| Site | Chromium (Playwright, headed in Xvfb) | Servo 0.7.0 (headless) |
|---|---|---|
| example.com | loads | loads |
| Google search | results | CAPTCHA |
| github.com/login | loads | loads |
| Walmart search | results | "The requested URL was rejected" |
| Amazon search | results | results |
| Target search | results | results |
| Best Buy | loads | does not load (`client error (SendRequest)`) |
| Instacart | loads | loads |
| LinkedIn sign-in | loads | loads |
| Reddit | blocked (the address, both engines) | blocked |
| bot.sannysoft.com | passes the rows checked | fails plugins, WebGL vendor, `PHANTOM_ETSL`; `navigator.languages` is `["C"]` |
| Start a browser | 0.9 to 1.5 s | 0.25 to 0.35 s |

Servo starts about four times faster, and simple pages load as fast or faster. But from the server's address it is
blocked where Chromium is not (Google, Walmart), fails a page Chromium loads (Best Buy), and its fingerprint is easy to
tell apart. It also blocked the internal address and the cloud metadata address (`10.128.0.2`, `169.254.169.254`), as
the proxy should.
