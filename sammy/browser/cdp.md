# The CDP backend: Chrome without Playwright

`ChromiumCDPBackend` in `cdp.py` runs the same Chrome as the Playwright backend (`chromium.py`), but drives it with our
own small Chrome DevTools Protocol client (`cdp_client.py`, about 160 lines) over `--remote-debugging-pipe`. No Node
driver, no Playwright at runtime, no port. Engine evaluation #18, next to `servo.md` and `lightpanda.md`.

```python
from sammy.browser.cdp import CDPOptions, ChromiumCDPBackend

browser = ChromiumCDPBackend(CDPOptions.server())  # the Linux server: headed on Xvfb, in bwrap
await browser.open(state)
```

Chrome is Playwright's Chromium (a Chrome for Testing build, found from Playwright's `browsers.json` without starting
Playwright), or `$SAMMY_CHROME_BINARY`. CI pins Chrome for Testing 153.0.8010.12 by SHA-256, the build Playwright
1.63 installs. `BROWSER_BACKEND=sammy.engines:chromium_cdp_server` runs it in the app; the default stays
`chromium_server`.

## How it works

| Part | How |
|---|---|
| Start | bash moves our two pipes to Chrome's fds 3 and 4 and execs Chrome (or bwrap, which passes them on). One Chrome and one throwaway profile per `open`; `close` kills the process group and deletes the folder |
| Navigation | `Page.navigate`, then the `load` lifecycle event of the document it committed, or of the one that replaced it before loading (Google's results page does) |
| Waiting after a click or key | Main-frame events (`frameStartedNavigating`, `frameNavigated`, `frameStoppedLoading`, `navigatedWithinDocument`) decide whether a navigation started and when it ended, as `chromium.py` does with Playwright's events |
| Input | `Input.dispatchMouseEvent`, `dispatchKeyEvent`, `insertText`: trusted events. Keys from `liveview/keys.py` |
| Scripts | Every script, the snapshot walker included, runs in an isolated world (`Page.createIsolatedWorld` + `Runtime.callFunctionOn`). The page cannot see it or its globals |
| Cookies | `Storage.getCookies` and `setCookies`, HttpOnly included. Domain cookies keep their leading dot both ways |
| localStorage | The current origin from the tab. Other origins (seeded, or visited) through a hidden tab (`Target.createTarget` with `hidden`) whose requests we answer ourselves with `Fetch`, so the site gets no request |
| sessionStorage | `Page.addScriptToEvaluateOnNewDocument` in our world, before the page's scripts, for the first load only |
| Screenshots | `Page.captureScreenshot` |
| Live view | `liveview/cdp.py`: `Page.startScreencast` on its own session per tab, tabs from target discovery, phone size with `Emulation.setDeviceMetricsOverride`, outline for screen readers |
| Downloads | `Browser.setDownloadBehavior` into the profile folder, `Browser.downloadWillBegin` and `downloadProgress` |
| Dialogs | Dismissed, as Playwright does; `beforeunload` is accepted so navigation goes on |
| Passkeys (WebAuthn) | Each tab and popup has an empty virtual security key and Chrome's passkey dialog is off, so a request fails at once (`NotAllowedError`) and the site offers another way to sign in. The dialog is Chrome's own window: the live view could not show it, and it blocked clicks on the page |

### Automation tells

| Tell | Playwright backend | CDP backend |
|---|---|---|
| `--enable-automation`, the "controlled by automated software" bar | removed by us | never passed |
| `navigator.webdriver` | false (`AutomationControlled` off) | false (same flag) |
| Runtime domain on the page (console messages get serialized, which pages detect) | on: Playwright enables it in every frame | never enabled; checked by `test_the_page_sees_no_automation` |
| Our scripts and their globals | Playwright's utility world, plus `page.evaluate` in the main world | only our isolated world |
| `__playwright*` bindings, `__pwInitScripts` | can appear | none |
| `data-sammy-ref` attributes from the snapshot walker | visible in the DOM | visible in the DOM (shared by every engine; not fixed here) |

Launch flags that no page can see keep the process count down: no background extensions, sync, phishing lists,
component updates or Chrome for Testing's built-in experiments. One tab runs 7 processes instead of 11.
`--allow-pre-commit-input` and focus emulation are needed headed on Xvfb, where the first keys after a load were lost
without them (Playwright passes both too).

## Conformance

All 16 tests of `BrowserBackendConformance` pass with no `not_supported`, export included, three ways:

| Where | Result |
|---|---|
| macOS, headless | 16/16 (headed: one manual run of open with state, click, type, screenshot, export, live view) |
| Linux CI (`chromium-cdp-linux`), headless | 16/16 |
| Linux in the server's jail: headed on Xvfb, bwrap, egress proxy (CI, and the GCP VM 3 times in a row) | 16/16 |

`tests/browser/test_cdp.py` also checks: domain and host-only cookies (with `--host-resolver-rules`), failed loads
leave the browser usable, no process outlives `close`, other origins' storage moves without a request, downloads from a
link and from `Navigate`, the live view (frames, input, outline, phone size, a held button let go), and, jailed, that
private and metadata addresses are refused and that no new TCP port listens on the host. The live view engine tests
(`tests/liveview/test_liveview_engines.py`) run on `cdp` too: 7 of 7.

## Engine evaluation (#18)

### End-to-end suite

`uv run --frozen pytest -q -p no:cacheprovider -n 4 tests/e2e --browser=...` on this Mac (scripted model, fixture
sites):

| | Chromium (Playwright, headless shell) | CDP (headless) | Servo 0.7.0 (`servo.md`) |
|---|---|---|---|
| Passed | 94 of 94 (140.6 s) | 94 of 94 (146.8 s) | 83 of 94 |
| Sign-in hand-off, saved sign-in, press-and-hold, downloads | pass | pass | fail |
| Live view | CDP screencast | CDP screencast, own session | polled screenshots |

The default suite (`--browser=fake`) still passes 94 of 94. CI runs the CDP suite on Linux in `chromium-cdp-linux`.

### Real sites from the server

On the GCP VM (datacenter address), the method of `servo.md`: a throwaway image from `sammy-app:latest`, each
engine jailed with its own egress proxy, a fresh browser per site, `open(None)`, `Navigate` (60 s), 5 s, then the page
read and a screenshot. Both Chromium columns are headed on Xvfb, same Chrome build. Every page text was read, not only
keyword matches. Times are open + navigate, seconds.

| Site | Chromium (Playwright) | Chromium (CDP) | Servo 0.7.0 (`servo.md`) |
|---|---|---|---|
| example.com | loads (1.0 + 0.3) | loads (0.9 + 0.3) | loads |
| Google search | results (1.3 + 5.1) | results (1.2 + 3.8) | CAPTCHA |
| github.com/login | loads (1.0 + 1.0) | loads (1.1 + 1.3) | loads |
| Walmart search | results (0.9 + 8.4) | results (1.0 + 7.2) | "The requested URL was rejected" |
| Amazon search | results (0.9 + 3.8) | results (1.1 + 3.3) | results |
| Target search | **"Press & hold to confirm you're a human"**, and "We couldn't find a match" (1.0 + 11.0) | results (0.9 + 6.5) | results |
| Best Buy | loads (0.9 + 5.6) | loads (0.9 + 5.0) | does not load |
| Instacart | loads (0.9 + 2.4) | loads (0.9 + 2.5) | loads |
| LinkedIn sign-in | loads (1.1 + 1.3) | loads (1.0 + 1.0) | loads |
| Reddit | "You've been blocked by network security" (the address, both) | same | blocked |
| nowsecure.nl | the page and its check iframe, nothing more in the text: inconclusive for both | same | not run |
| bot.sannysoft.com | every row passes but WebGL Renderer (SwiftShader) | the same rows, the same results | fails plugins, WebGL vendor, `PHANTOM_ETSL`, languages |
| 10.128.0.2, 169.254.169.254 | refused (`ERR_SOCKS_CONNECTION_FAILED`) | refused (same) | refused |

sannysoft rows asked for: WebDriver "missing (passed)", Chrome "present (passed)", Plugins Length 5, Languages
`en-US,en`, WebGL Vendor "Google Inc. (Google)", every `PHANTOM_*` and `HEADCHR_*` row "ok", for both Chromium columns.
WebGL Renderer is SwiftShader for both, which a careful site can read as "no GPU".

Target challenged Playwright and not CDP in both full runs (two runs, 15 minutes apart). That is two samples from one
address, so it is a hint, not proof, that the Runtime domain and Playwright's main-world scripts are what PerimeterX
picks up. The Servo run, earlier, saw Target results with Playwright.

A first run found a real bug: Google replaced its results page before the load event, and `Navigate` waited the full
60 s. Fixed (see "Navigation" above); the table is the second run.

### Speed and memory

`tests/browser/bench_cdp.py`, medians, the conformance fixture site. Server: the VM, both jailed and headed on Xvfb,
5 runs. The VM was shared with another agent's evaluation; a first run during its load showed CDP 2 to 3 times slower
on every step, the run below had a load average under 3. Mac: headless, 10 runs, both on Playwright's full Chrome (not
the headless shell).

| Step, seconds | Server: Playwright | Server: CDP | Mac: Playwright | Mac: CDP |
|---|---|---|---|---|
| start (`open(None)`) | 0.95 | 0.95 | 0.58 | 0.82 |
| open with the conformance state | 1.48 | 1.26 | 0.94 | 0.74 |
| navigate | 0.059 | 0.051 | 0.025 | 0.030 |
| snapshot | 0.023 | 0.026 | 0.017 | 0.016 |
| click | 0.061 | 0.070 | 0.060 | 0.116 |
| type into a selector | 0.155 | 0.071 | 0.091 | 0.060 |
| screenshot | 0.074 | 0.061 | 0.095 | 0.170 |
| export | 0.180 | 0.089 | 0.066 | 0.060 |
| close | 0.155 | 0.037 | 0.102 | 0.028 |
| RSS of the browser's processes, MiB | 1143 | 1123 | 968 | 952 |

On the real sites, opening a browser took 0.9 to 1.2 s for both (CDP 0.92 to 1.20, Playwright 0.91 to 1.32), and the
RSS sums with a real page loaded were within 1 to 10 % of each other (1.1 GiB on example.com, 2.1 to 2.5 GiB on Target
and Best Buy). RSS counts shared pages once per process, so it overstates a single browser.

What the numbers say: Chrome's own start dominates, and it is the same binary, so starting is no faster. Calls that
Playwright does in several round trips (fill, storage state, closing the context) are 2 to 4 times faster; single
calls are the same within noise. What is gone for good is the Playwright Node driver: one fewer process per app
(its memory was not measured here) and no JSON hop through it.

### What the agent loses

Nothing. Unlike Servo and Lightpanda, this is the same Chrome:

| | Playwright backend | CDP backend |
|---|---|---|
| Export, `release` (HttpOnly cookies) | yes | yes |
| Downloads (#21) | yes | yes |
| Live view (#14), with tabs, phone size and outline | yes | yes |
| Screenshots | yes | yes |
| Hand-off and saved sign-ins | yes | yes |
| WebSockets on the server | through the proxy | through the proxy |

What is ours to maintain instead of Playwright's: the waits for navigation, the selector clicks (no actionability
checks beyond "visible"; Playwright also waits for the element to be stable and enabled), the US keyboard map (shifted
characters are sent as text, without Shift held), dialogs, and new CDP behaviour with each Chrome release.

## Verdict

Make it the default after a short soak, not today. It passes everything the Playwright backend passes, is at least as
fast, uses no Node driver, and is harder to detect (Target let it through twice when it challenged Playwright). To get
there:

1. Run it next to `chromium_server` on real runs for a week (`BROWSER_BACKEND` per deployment, or a share of runs), and
   compare challenges and failed actions.
2. Pin the Chrome build ourselves (Chrome for Testing, by SHA-256, as CI does) instead of borrowing Playwright's, so
   Playwright can leave the app image's runtime.
3. Add Playwright's actionability waits that agents need on real sites (stable, enabled, not covered) to `_find`.
4. Stop stamping `data-sammy-ref` on the page's elements (keep the map in our isolated world only), for every
   engine.

## Not verified

- Headed on macOS beyond one manual run (open with state, click, type, screenshot, export, live view): the suite runs
  headless on the Mac, headed only on Linux.
- Linux on arm64: Chrome for Testing has no build; Playwright's own Chromium would be used.
- The jail cannot run under Podman on this Mac (as for Chromium and Servo); it ran in CI and on the VM.
- nowsecure.nl's result could not be read from the page text, for either engine.
