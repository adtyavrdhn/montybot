# Browser engine spike

Measures six browser engines against monty-bot's five requirements, next to the earlier Servo 0.7.0 spike
(`../servo`). Everything ran on the same M-series Mac (macOS 26.5, arm64) on 2026-10-06.

| Engine | Version | How it was driven |
|---|---|---|
| Chromium headless shell | chrome-headless-shell 153.0.8010.12 | Playwright 1.63.0, `chromium.launch(headless=True)` |
| Chromium new headless | Chrome for Testing 153.0.8010.12 | Playwright 1.63.0, `chromium.launch(channel="chromium", headless=True)` (the documented opt-in to new headless) |
| Firefox | Playwright Firefox 155.0 | Playwright 1.63.0, `firefox.launch()` |
| WebKit | Playwright WebKit 26.6 | Playwright 1.63.0, `webkit.launch()` |
| Lightpanda | 1.0.0 release binary, `lightpanda-aarch64-macos` | `lightpanda serve` with `LIGHTPANDA_DISABLE_TELEMETRY=true`, then Playwright `connect_over_cdp` |
| Camoufox | camoufox 0.5.7, browser 156.0.1-beta.34 | `camoufox.sync_api.NewBrowser(playwright, headless=True)` (it pins Playwright 1.62.0) |
| Servo (earlier spike) | servo crate 0.7.0 | Rust embedding, see `../servo/README.md` |

## Setup

Nothing was installed system-wide. All browsers live in `bin/` (gitignored, about 3.6 GB):

```sh
cd spikes/engines
# Playwright browsers into bin/ms-playwright
PLAYWRIGHT_BROWSERS_PATH=$PWD/bin/ms-playwright uv run --no-project --with playwright==1.63.0 \
  playwright install chromium chromium-headless-shell firefox webkit
# Lightpanda 1.0.0 into bin/lightpanda (sha256 9554400...8173d)
gh release download 1.0.0 -R lightpanda-io/browser -p lightpanda-aarch64-macos -D bin/lightpanda
chmod +x bin/lightpanda/lightpanda-aarch64-macos
# Camoufox: HOME is pointed into bin/ so its platformdirs cache lands there (2.3 GB, mostly the browser)
HOME=$PWD/bin/camoufox-home UV_CACHE_DIR=~/.cache/uv uv run --no-project --with camoufox==0.5.7 python -m camoufox fetch
```

## Run

```sh
python3 scripts/server.py 8766 &       # serves ../servo/site/login.html plus /set (HttpOnly cookie) and /check (echoes Cookie)
scripts/run_all.sh                      # runs + checks for the 4 Playwright engines and Lightpanda
HOME=$PWD/bin/camoufox-home UV_CACHE_DIR=~/.cache/uv uv run --no-project --with camoufox==0.5.7 --with psutil \
  python scripts/measure.py --engine camoufox --phase runs      # and --phase checks
PLAYWRIGHT_BROWSERS_PATH=$PWD/bin/ms-playwright uv run --no-project --with playwright==1.63.0 --with psutil \
  python scripts/lightpanda_extra.py    # Lightpanda-only follow-ups (one context per connection)
python3 scripts/summarize.py            # per engine x site medians from out/results.jsonl
```

## Method

**Timed runs** (`measure.py --phase runs`). Every run launches a fresh browser process, opens one context and one page,
calls `page.goto(url, wait_until="load", timeout=60s)`, waits 2 s, then reads the title, URL and DOM, and saves a
screenshot to `out/<engine>-<site>-<run>.png`. Each engine got one unrecorded warm-up launch first, because the first
launch after install pays macOS first-run checks. The sites were example.com, github.com/login, the local login page
and walmart.com, three runs each. A fourth walmart load per engine went to `walmart.com/search?q=milk`, because the
earlier Servo spike showed the homepage alone is a weak bot test. Lightpanda got a fifth walmart load, a re-check of
that search page.

- **launch s**: from calling `launch()` until Playwright has a connected `Browser`. For Lightpanda this covers
  spawning the process, polling `/json/version` until it answers (45-65 ms) and then `connect_over_cdp`.
- **load s**: from `goto()` until the load event. Servo's number is from spawning the process to `LoadStatus::Complete`,
  so it includes startup.
- **Memory.** A psutil thread samples every 50 ms. Each sample takes every descendant of the Python process except
  the Playwright driver, plus any process started after launch whose executable lives under the engine's install dir.
  The second rule catches WebKit's XPC helpers, which macOS launchd starts rather than the browser. It reports two
  numbers:
  - **peak RSS sum**: the largest sum of RSS across that tree. This overcounts badly on macOS because every helper
    process maps the same 200+ MB framework `__TEXT`. Some samples also caught the ~10 MiB `footprint` tool the
    script itself spawns (this is fixed in the script now, after the runs). Read it as an upper bound.
  - **peak footprint**: the macOS `footprint` tool, run on the tree after the 2 s settle, summing each process's
    `phys_footprint_peak`. That counts dirty memory and leaves out shared clean pages. It is the same kind of number
    as the "peak memory footprint" `/usr/bin/time -l` gave for Servo, so **compare engines on this column**.
- **challenge**: the title or DOM contains "Robot or human", the DOM contains `px-captcha`, or the URL contains
  `/blocked`. Every hit was checked against its screenshot.

**Functional checks** (`measure.py --phase checks`, one browser per engine, out/checks-<engine>.json):

1. Accessibility. Runs `page.locator('body').aria_snapshot()` on the login page, clicks `#user`, types "alice"
   through `page.keyboard` and takes the snapshot again (`out/<engine>-aria-*.txt`). It also runs
   `page.aria_snapshot(mode="ai")`, and `Accessibility.getFullAXTree` on CDP engines.
2. storage_state. `/set` sets the HttpOnly cookie `sid`, then `localStorage.setItem` adds a value,
   `context.storage_state()` exports both, and they are imported into a fresh context with `new_context(storage_state=...)`.
   The check passes when `/check` receives the cookie server-side and `localStorage` reads back.
3. Takeover. Uses Playwright 1.63's cross-engine `page.screencast.start(on_frame=...)` while `page.mouse` and
   `page.keyboard` click into the field, type, and click the submit button. CDP engines also get raw
   `Page.startScreencast` plus `Input.dispatchMouseEvent` and `Input.dispatchKeyEvent`.
4. Density. Opens 5 more contexts in the same process, each with a page on the login page. It checks that a cookie
   set in one context doesn't reach `/check` in another, and measures footprint before and after.

**Egress caveat.** Walmart's own UI placed this connection in the US (a Sacramento store and a "Virginia Health
Information Consent" banner), so the egress was not a plain Dutch residential line. It was described to us as
residential, and I couldn't verify that. Either way it is a consumer-grade IP, so bot-check results here are
**optimistic** next to a datacenter host. All engines also ran back-to-back from one IP, so later loads may have
inherited a worse risk score, and 3+1 loads per engine is a small sample.

## Comparison table (measured)

Medians of 3 runs. Ranges and every individual run are in `out/results.jsonl` (`python3 scripts/summarize.py`).
"fp" is peak footprint in MiB.

| Engine | launch s | load s: example / github / walmart | fp MiB: login / example / github / walmart | peak RSS sum MiB: example / walmart | walmart home challenge | walmart search | a11y: roles, names, typed value | storage_state round trip (HttpOnly + localStorage) | live view + input | contexts per process | fp per extra context |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Chromium headless shell | 0.061 | 0.30 / 0.65 / 2.38 | 50 / 64 / 123 / 314 | 311 / 698 | 0/3 | blocked page (`/blocked`, press & hold) | yes / yes / yes | yes / yes | CDP screencast yes, PW screencast yes, input yes | many, isolated | 20 MiB |
| Chromium new headless | 0.157 | 0.34 / 0.69 / 2.11 | 531 / 549 / 606 / 882 | 964 / 1518 | 0/3 | blocked page | yes / yes / yes | yes / yes | CDP yes, PW yes, input yes | many, isolated | 128 MiB |
| Firefox | 0.358 | 0.32 / 1.01 / 1.98 | 363 / 384 / 467 / 655 | 1053 / 1339 | 1/3 (modal) | results under a press & hold modal | yes / yes / yes | yes / yes | PW screencast yes (~22 fps), input yes, no CDP | many, isolated | 74 MiB |
| WebKit | 0.120 | 0.25 / 0.87 / 2.60 | 183 / 187 / 275 / 474 | 349 / 680 | 1/3 (modal) | blocked page | yes / yes / yes | yes / yes | PW screencast yes, input yes, no CDP | many, isolated | 23 MiB |
| Lightpanda 1.0.0 | 0.049 | 0.29 / 0.68 / 2.08 | 10 / 10 / 39 / 114 | 42 / 156 | 1/3 (challenge text) | 0/2, real results (61 "Add to cart" buttons in snapshot) | yes / yes / yes (`aria_snapshot`, and AX tree with `{}` params) | export yes; **import via storage_state drops cookies** (localStorage yes) | **no screencast**; screenshots are a text-only render; input yes | **1 context per CDP connection**, several connections per process (isolated) | n/a |
| Camoufox 0.5.7 | 0.40 (macOS fingerprint) / 2.82 (Win/Linux fingerprint) | 0.81 / 1.02 / 2.17 | 634 (macOS fp) to 1468 / 1471 / 740-1554 / 1818 | 2922 / 2899 | 1/3 modal, 1/3 load timeout | load timeout (60 s) | yes / yes / yes | yes / yes | PW screencast yes (~22 fps), input yes, no CDP | many, isolated | 276 MiB |
| Servo 0.7.0 (earlier) | n/a, in-process | 0.35-0.49 / 0.77-1.12 / 1.4-2.2 (includes startup) | 308 / 308 / 399 / 505-674 (`/usr/bin/time -l` peak footprint) | maxRSS 101 / 314-438 (single process) | 6/7 | not tested | **no** roles, names or values for input and button | cookies via `site_data_manager` (HttpOnly ok); no storage_state shape | screenshot API + `notify_input_event`, no screencast stream | **1 instance per process** | n/a |

Other measured facts:

- **Bot-relevant identity.**
  - Every Playwright-launched engine reports `navigator.webdriver === true`.
  - Both Chromium modes send `HeadlessChrome/153` in the UA. That includes new headless (`HeadlessChrome/153.0.0.0`).
  - Lightpanda sends `User-Agent: Lightpanda/1.0` and `webdriver=false`. Its `--user-agent` flag forbids any value
    containing "Mozilla", so it can't present itself as a mainstream browser.
  - Camoufox reports `webdriver=false` and picks a random OS fingerprint per launch (Windows, Linux or macOS UA).
- **Lightpanda loads no sub-resources by default.** Images, stylesheets, iframes and workers are all off, and the log
  says `iframes disabled` and `workers disabled` on walmart. You enable them with `--load-resources`. This explains a
  large part of its memory and speed lead. Its "screenshot" (`Page.captureScreenshot` works) is plain text with no
  layout and no form controls drawn (`out/lightpanda-login-1.png`).
- **Camoufox's spoofed OS costs a lot on this Mac.** With a non-macOS fingerprint, launch goes from 0.40 s to 2.8 s
  and footprint from about 700 MiB to about 1470 MiB, every time. It runs 7-8 processes.
- **Chromium new headless runs a GPU helper on macOS.** About 209 MB of its footprint is "Owned physical footprint
  (graphics)", which is why it is roughly 10x the headless shell at idle. This may not hold on a Linux server without
  a GPU (not measured).
- **Screencast frame rate.** Over about 2.7 s of typing, Chromium sent 12-16 frames, because it only sends frames when
  something changes and the CDP test acks every 100 ms. Firefox and Camoufox sent about 60 frames, WebKit 15. The first
  frame arrived within 4-140 ms everywhere it worked.

## Exact errors

- Lightpanda, `browser.new_context()` while a context exists:
  `Error: Browser.new_context: Protocol error (Target.createBrowserContext): Cannot have more than one browser context at a time`
- Lightpanda, raw CDP screencast:
  `Error: CDPSession.send: Protocol error (Page.startScreencast): 'Page.startScreencast' wasn't found`.
  Playwright's `page.screencast.start()` on Lightpanda raised nothing and delivered 0 frames.
- Lightpanda, `Accessibility.getFullAXTree` with no params object:
  `Error: CDPSession.send: Protocol error (Accessibility.getFullAXTree): InvalidParams`. With `{}` it works and returns
  `textbox "Username" value "alice"`, `textbox "Password" value "*****"` and `button "Log in"`.
- Lightpanda, `DOMSnapshot.captureSnapshot`: `'DOMSnapshot.captureSnapshot' wasn't found`.
- Lightpanda, cookie import. `new_context(storage_state=...)` and `context.add_cookies()` both succeed silently, but
  `Network.getAllCookies` stays `[]` and the server receives no cookie. Playwright uses `Storage.setCookies` with a
  `browserContextId`. Calling `Network.setCookie` on the page's CDP session **does** work: the server then receives
  `sid=spike-httponly`. Export works (`Storage.getCookies` and `Network.getAllCookies` both return the HttpOnly cookie).
- Camoufox, walmart.com run 2 and walmart search: `TimeoutError: Page.goto: Timeout 60000ms exceeded.` (no load event
  within 60 s).
- Chromium: `Storage.getCookies` on a page-level CDP session returned `[]` while `Network.getAllCookies` returned the
  cookie. That's expected, because `Storage.getCookies` needs the browser target. Playwright's `storage_state` is
  unaffected.

## What we would have to build (per engine, against the 5 requirements)

### Chromium headless shell
1. **Bot protection.** Passed the walmart homepage 3/3 but was hard-blocked on search. To fix:
   - Remove the obvious tells: a real UA without `HeadlessChrome`, `--disable-blink-features=AutomationControlled`
     or an init script, and consistent client hints.
   - Keep one persistent profile per user so PerimeterX cookies (`_px*`) build up trust.
   - Send egress through residential or ISP proxies, and route press-and-hold to human takeover.
   - Re-measure from a datacenter host.
2. **Accessibility snapshot.** Nothing to build. `aria_snapshot(mode="ai")` gives roles, names, values and `[ref=eN]`.
3. **Takeover.** CDP `Page.startScreencast` plus `Input.dispatch*` work. The relay already exists in `../takeover`.
4. **Cookie jar.** Nothing to build. `storage_state()` round-trips HttpOnly cookies and localStorage.
5. **Density.** Build a per-user process supervisor with memory limits. It is the cheapest real renderer measured:
   50-64 MiB idle, about 314 MiB on walmart, 60 ms launch, about 20 MiB per extra context. Re-measure on Linux.

### Chromium new headless
1. Same bot work as the headless shell. It also sends `HeadlessChrome` and was blocked identically on search, so the
   new headless mode gave no measurable bot advantage here.
2. Nothing to build for accessibility, takeover or cookies (same as above).
3. Nothing to build.
4. Nothing to build.
5. On macOS it costs about 8x the headless shell in footprint (GPU helper). Check Linux with `--disable-gpu` before
   ruling it in or out. Today it only makes sense where full-Chrome fidelity is needed, for example extensions or
   WebGL fingerprint consistency.

### Firefox (Playwright build)
1. Challenged with a modal on 1/3 homepage loads and on search. Needs the same proxy, persistent profile and takeover
   work, plus Firefox-specific fingerprint hygiene. `navigator.webdriver` is true.
2. Nothing to build for accessibility.
3. **Takeover.** Rebuild the relay on Playwright's `page.screencast` and `page.mouse`/`keyboard` (Juggler protocol)
   instead of raw CDP. It worked at about 22 fps.
4. Nothing to build for cookies.
5. 363-655 MiB footprint, 0.36 s launch, 74 MiB per extra context. The heaviest stock engine after new headless.

### WebKit (Playwright build)
1. Blocked on search and modal on 1/3 homepage loads. Same mitigations as above. The Linux WebKit build will
   fingerprint very differently from Safari on macOS, so its bot behaviour has to be re-measured on Linux.
2. Nothing to build for accessibility.
3. **Takeover.** Relay on `page.screencast` and `page.mouse`/`keyboard`, as for Firefox.
4. Nothing to build for cookies.
5. 183-474 MiB footprint, 0.12 s launch, 23 MiB per extra context. On macOS its helpers are launchd XPC services, so
   per-user cgroup accounting on Linux needs to be confirmed.

### Lightpanda 1.0.0
1. **Bot protection.** Got real walmart search results 2/2, but showed the challenge text on 1/3 homepage loads. It
   announces itself as `Lightpanda/1.0`, its `--user-agent` flag rejects any value containing "Mozilla", and it skips images, CSS, iframes and
   workers by default. So it likely passes today because PerimeterX's sensor partly doesn't run. Expect that to change
   once Lightpanda is popular enough to be fingerprinted. Logged-in flows and checkout were not tested. Work list:
   - Turn on `--load-resources iframe,worker,stylesheet` and re-measure.
   - Build a fallback to a real engine when a challenge appears, because a human can't solve press-and-hold on a page
     Lightpanda can't draw.
2. **Accessibility snapshot.** Works through Playwright's `aria_snapshot` (including AI-mode refs) and through
   `Accessibility.getFullAXTree({})`. Nothing to build.
3. **Takeover.** Not possible. There is no screencast, and screenshots are text-only. Takeover needs a hand-off: export
   cookies and localStorage, open the same session in a real engine for the human, then import back. That needs
   working import (see 4).
4. **Cookie jar.** Write our own import with `Network.setCookie` or `setCookies` per page session, because
   `storage_state` import and `add_cookies` silently drop cookies. Export works. localStorage import works.
5. **Density.** Best measured by far: 10 MiB idle, 39 MiB on github, about 114 MiB on walmart, 49 ms to a ready CDP
   server, one process. Only one context per CDP connection. Isolated users either get one process each (cheap
   anyway) or one connection each against `--cdp-max-connections` (default 16). That second option was verified
   isolated for 2 connections.

### Camoufox 0.5.7
1. **Bot protection.** No better than stock engines in this sample: one modal, plus two 60 s load timeouts on walmart.
   Needs:
   - Fingerprint pinning per user (a stable OS, and the host's own OS to avoid the font cost above).
   - Proxy and geo coherence. Camoufox has GeoIP helpers.
   - A fix or understanding of the load timeouts.
2. Nothing to build for accessibility.
3. **Takeover.** Relay on `page.screencast` (about 22 fps here), as for Firefox.
4. Nothing to build for cookies.
5. **Density.** Worst measured: 634-1818 MiB footprint per process, 276 MiB per extra context, 0.4-2.8 s launch,
   7-8 processes. It also pins its own Playwright (1.62) and a 2.3 GB download. Running it at scale means tracking a
   beta Firefox fork.

### Servo 0.7.0 (from the earlier spike, not re-measured)
1. Challenged 6/7 on the walmart homepage, and walmart needed experimental prefs to run. Needs engine work on web
   compatibility and fingerprint before bot defence is even testable.
2. **Accessibility.** The AccessKit tree had no roles, names or values for input and button. Needs upstream
   accessibility work in Servo.
3. **Takeover.** Build a frame pump on `take_screenshot` / `notify_new_frame_ready` plus input mapping.
   No screencast stream exists.
4. **Cookie jar.** Build a storage_state adapter on `site_data_manager` cookies. localStorage export was not shown.
5. **Density.** Single process but 308-674 MiB peak footprint, and one instance per process.

## Opinion (not measured)

- **Default engine: Chromium headless shell.** It is the only engine that meets requirements 2-4 with zero work, it is
  the cheapest real renderer here (50-314 MiB footprint, 60 ms launch), and the existing takeover spike already speaks
  its protocol. Its bot gap (`HeadlessChrome`, `webdriver=true`) is the best documented and the most fixable of the
  set. On this sample, new headless bought nothing for bot checks and cost about 8x the memory on macOS.
- **Lightpanda is worth keeping as a cheap tier, not as the engine.** It works for read-mostly agent steps on sites
  that don't challenge, at a tenth of the memory. But it can't support human takeover at all, it needs a custom cookie
  import, and it is an openly identified bot UA. Its walmart pass reflects what it doesn't execute more than evasion.
  If we use it, it needs a "switch to Chromium" escape hatch that carries the session over.
- **Requirement 1 isn't settled by the engine choice.** Every engine was challenged by PerimeterX at least once from a
  consumer IP. The deciding factors will be egress (residential or ISP proxies), persistent per-user profiles,
  fingerprint hygiene and fast human takeover for press-and-hold. Measure those from a datacenter host next.
- **Firefox, WebKit, Camoufox and Servo** offer no advantage here that pays for their extra memory, their lack of a
  CDP relay, or (Servo) their missing accessibility semantics.

## Files

- `scripts/server.py`: local login page, plus `/set` and `/check` for the HttpOnly round trip
- `scripts/measure.py`: timed runs and functional checks, per engine
- `scripts/run_all.sh`: runs every Playwright engine and Lightpanda
- `scripts/lightpanda_extra.py`: Lightpanda cross-process storage import and two-connection isolation
- `scripts/summarize.py`: the per-site table from `out/results.jsonl`
- `out/results.jsonl`: every timed run, with UA, `webdriver`, process names, both memory metrics and the challenge verdict
- `out/checks-<engine>.json`, `out/lightpanda-extra.json`: functional checks
- `out/*.png`, `out/*.jpg`, `out/*-aria-*.txt`, `out/*-storage-state.json`: screenshots, screencast frames,
  snapshots, exported state. The storage-state files hold only the local test cookie.
- `logs/`: per-phase stdout/stderr and the Lightpanda server logs
