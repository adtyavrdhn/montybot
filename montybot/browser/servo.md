# The Servo backend

`ServoBackend` in `servo.py` runs Servo behind the browser contract (`contract.py`), over servoshell's built-in W3C
WebDriver. Issue #12. Tested with Servo 0.7.0 on macOS (Apple Silicon). Linux is code and docs only, not run.

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
  each test's Servo is reachable from the LAN while it runs. On Linux, pasta gives Servo its own network namespace and
  forwards only the host's `127.0.0.1:PORT` (`servo_bwrap.py`). A one-line upstream change would fix it at the source.
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

## Linux in bwrap (not run)

`servo_bwrap.py` has `BwrapLauncher`, for `ServoOptions(launcher=BwrapLauncher())`. It runs servoshell inside bwrap
(own user, pid, IPC and UTS namespaces, host files read-only, private `/tmp`, only the profile folder writable,
`--die-with-parent`), inside pasta's network namespace with only the WebDriver port forwarded to the host's loopback.
Headless Servo renders in software, so it needs no Xvfb. To check on the server from #7:

- the release tarball's layout, and which `/usr` libraries (Mesa) and fonts it needs;
- that `pasta -t 127.0.0.1/PORT` forwards to Servo's `0.0.0.0` listener, and that DNS works inside (`/etc/resolv.conf`
  may point at a resolver pasta has to forward);
- that SIGKILL to the process group (pasta leads it) takes bwrap and Servo down;
- blocking private address ranges, as `DESIGN.md` asks for Chromium;
- Ubuntu 24.04's AppArmor block on unprivileged user namespaces.
