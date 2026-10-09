# The Lightpanda backend

`LightpandaBackend` in `lightpanda.py` runs [Lightpanda](https://github.com/lightpanda-io/browser) behind the browser
contract (`contract.py`), over its CDP server (`lightpanda serve`). Engine evaluation (#18), next to Servo's
(`servo.md`). Tested with Lightpanda 1.0.0 on macOS (Apple Silicon) and Ubuntu 24.04 (aarch64 locally, x86_64 on the
server and in CI's `lightpanda-linux` job).

```python
from sammy.browser.lightpanda import LightpandaBackend, LightpandaOptions

browser = LightpandaBackend(LightpandaOptions())
await browser.open(state)
```

Get the binary for your platform from the [1.0.0 release](https://github.com/lightpanda-io/browser/releases/tag/1.0.0)
(`lightpanda-x86_64-linux`, `lightpanda-aarch64-linux`, `lightpanda-aarch64-macos`; GitHub lists each asset's
SHA-256). The backend looks for it at `$SAMMY_LIGHTPANDA_BINARY`, else `~/.cache/sammy/lightpanda/lightpanda`.
CI pins `lightpanda-x86_64-linux` by its SHA-256, `aa5a4b8e...031c3`.

Lightpanda is AGPL-3.0. Sammy runs the unmodified release binary as a separate process and talks to it over CDP, a
network protocol; nothing of Lightpanda is linked, vendored or changed. That is the usual reading of "aggregate" use,
and the AGPL's network clause (section 13) applies to a modified Lightpanda that users interact with, which this is
not. Mike should still check it before anything ships.

## Conformance

All 19 tests of `BrowserBackendConformance` pass against Lightpanda 1.0.0, unjailed on macOS and Linux, with
`not_supported = {'screenshot', 'point', 'mouse', 'scroll'}`. The jailed run is checked in CI (Podman on this Mac
cannot run bwrap, as for Chromium and Servo).

| Feature | Status | Why |
|---|---|---|
| open, seeding (HttpOnly too), storage | passes | `Network.setCookies`, then storage written on intercepted pages (below) |
| `export`, `release` | passes | `Network.getAllCookies` returns HttpOnly cookies with domain and SameSite |
| navigate, click and type on selectors and refs, press | passes | `Input.dispatchMouseEvent` and `Input.dispatchKeyEvent` |
| `screenshot` | `NotSupported` | `Page.captureScreenshot` returns a PNG, but it draws the text only, in black on white, at places that match neither the site's design nor Lightpanda's own boxes |
| `point`, `mouse`, `scroll` | `NotSupported` | No layout engine: `getBoundingClientRect` is a plain block flow that ignores CSS positioning, and the page cannot scroll. A point means nothing a person or a model could have seen |

## How it works, and the decisions

1. **One process per `open`**, `lightpanda serve` on a free loopback port, with `--disable-metrics` and
   `LIGHTPANDA_DISABLE_TELEMETRY=true`. Without that variable Lightpanda sends usage reports to
   `telemetry.lightpanda.io` (seen through a test proxy while probing). `close()` sends SIGKILL to the process group
   and deletes the profile folder.
2. **One page, no side tab.** A Lightpanda browser context holds one page (`TargetAlreadyLoaded` for a second one).
   So storage is reached another way:
   - **Seeding:** `open` loads `ORIGIN/__sammy_storage__` for each origin with storage, with `Fetch` interception
     answering it with an empty page. No request reaches the site. The state's own origin goes last, so its
     sessionStorage is the tab's when `state.url` loads. These pages stay in the tab's history.
   - **Export:** the current origin is read from the page. Every other seeded or visited origin is read from a hidden
     iframe on the same intercepted page, added to the current document and removed straight after. A page's own
     scripts could see that iframe for a moment.
3. **Host-only cookies are set by URL.** Like Chrome, Lightpanda turns a `domain` into a domain cookie (`localhost`
   became `.localhost`).
4. **`--load-resources stylesheet iframe`.** Lightpanda skips external stylesheets and iframes by default. Without
   stylesheets the snapshot would show what CSS hides; without iframes it would leave out same-origin frames, and
   export could not reach other origins.
5. **Navigations a script replaces are followed.** Google's results page starts a second navigation from a script,
   and Lightpanda then reports the first document's request as failed (`Shutdown`) after it was shown. Only a
   request that fails before its document is shown fails `Navigate`.
6. **The user agent stays `Lightpanda/1.0`.** Lightpanda refuses any `--user-agent` that contains `Mozilla` ("must
   not impersonate other browsers"), and `Network.setUserAgentOverride` is accepted but has no effect. We kept that.
7. **`snapshot.js`** no longer throws on a document without any element, which is what Lightpanda's `about:blank` is.

Found while building it:

- `autofocus` is ignored (as in Servo), but Enter in a field submits its form, and Tab moves the focus.
- A mouse move with the button held fires no `mousemove`, and a wheel event does not scroll.
- The CDP server listens on 127.0.0.1 only (Servo's WebDriver listens on every interface).
- `Page.navigate` reports a failure in its reply (`errorText`), and Lightpanda then shows its own "Navigation failed"
  page on `about:blank`.

## Linux, in the server's jail

`LightpandaOptions(bwrap=True)`, or `BROWSER_BACKEND=sammy.engines:lightpanda_server`, runs Lightpanda in
Chromium's jail (`chromium_linux.bwrap_command`): its own user, pid, IPC, UTS and network namespaces, host files
read-only, only the profile folder writable, `--die-with-parent`, an empty environment but for
`LIGHTPANDA_DISABLE_TELEMETRY`. Two socat processes inside are its only connections to the host:

- **Out:** `--http-proxy socks5h://127.0.0.1:1080`, which socat forwards to the `EgressProxy` (`egress.py`). Lightpanda
  fetches with libcurl, which sends every request through the proxy, `http://` and loopback included, as SOCKS5 with
  the host name unresolved, so the proxy does the DNS lookup and the address check. With an `http://` proxy, libcurl
  would send plain `GET http://...` for `http://` pages, which the egress proxy does not take.
- **In:** the CDP port is published only as `cdp.sock` in the profile folder (mode 600). The backend connects its
  websocket over that Unix socket.

The release binary needs only glibc. On the server it refused `http://10.128.0.2/` and `http://169.254.169.254/`
(`could not load ...: Proxy`). `test_jailed_lightpanda_reaches_no_private_address_and_no_host_port` checks the same in
CI, plus that nothing answers on the CDP port from the host.

## Engine evaluation (#18)

### End-to-end suite

`uv run --frozen pytest -q -p no:cacheprovider -n 4 tests/e2e --browser=lightpanda` (scripted model, fixture sites), on
macOS, unjailed, twice with the same result:

| | Chromium (`--browser=chromium`) | Servo 0.7.0 | Lightpanda 1.0.0 |
|---|---|---|---|
| Passed | 94 of 94 | 83 of 94 | 83 of 94 |
| Sign-in hand-off, and everything after it | passes | fails: `autofocus` and Enter | fails: no live view (no screenshots), so the live view closes at once (code 4410) |
| Sign-in saved for the next run | passes | fails: no export | fails at the hand-off before it; export itself works |
| Press-and-hold check in the live view | passes | fails | fails: no live view, and no mouse |
| Downloads (`test_files`) | passes | fails | fails: no downloads, and the ref went stale when the file opened as a page |
| Live view | CDP screencast | polled screenshots, about 20 fps | none |

The 11 failures: 10 need the live view (`test_paths` press and hold, three in `test_sign_ins`, three in
`test_skeleton`, the weekly cart in `test_schedules`, and both take-overs in `test_web_app`), and one is the download.
Everything else passes, including the agent's own reading, searching, refs, forms and approvals.

### Real sites from the server

On the GCP VM (datacenter address), both engines jailed with their own egress proxy, a fresh browser per site, the page
read 5 seconds after it loaded. Same method as Servo's; Lightpanda's last full run, with Chromium straight after:

| Site | Chromium (Playwright, headed in Xvfb) | Lightpanda 1.0.0 |
|---|---|---|
| example.com | loads | loads |
| Google search | results | results (the simpler page Google sends other browsers) |
| github.com/login | loads | loads |
| Walmart search | results | results ("Results for eggs (85)", with prices) |
| Amazon search | results | blocked: "Sorry! Something went wrong!" (the dogs page), every run |
| Target search | loads, but "We couldn't find a match for your search", both runs | results ("228 results for eggs") |
| Best Buy | loads | does not load: `Http2Stream`; with `--http-version 1.1` it timed out instead |
| Instacart | loads | loads |
| LinkedIn sign-in | loads | loads |
| Reddit | blocked (the address, both engines) | blocked |
| nowsecure.nl | the same text for both: the check runs in a frame the snapshot cannot read, so not conclusive | same |
| bot.sannysoft.com | passes the rows checked | see below |
| 10.128.0.2, 169.254.169.254 | refused | refused |

bot.sannysoft.com with Lightpanda: WebDriver passed (missing), Chrome failed (missing), Plugins length 0 and "is of
type PluginArray" failed, Languages `en-US,en` (correct, where Servo had `C`), WebGL vendor and renderer "Canvas has no
webgl context", every PHANTOM row ok except `PHANTOM_WINDOW_HEIGHT` (no outer window), every HEADCHR row ok,
`CHR_MEMORY` failed. The user agent says `Lightpanda/1.0` in plain text, so any site that wants to can refuse it.

In an earlier run that same hour, after many Google searches from the VM, Google once sent Lightpanda to its
`/sorry` CAPTCHA page; Chromium was not tested at that moment.

### Speed and memory

Server (the VM, x86_64, 4 cores, jailed, one browser at a time, 14 sites each). Memory is the PSS of every browser
process 5 seconds after the page loaded, from `/proc/PID/smaps_rollup`, so shared pages count once:

| What | Chromium (headed in Xvfb) | Lightpanda |
|---|---|---|
| Start a browser (`open(None)`) | median 1.01 s (0.92 to 1.72) | median 0.08 s (0.08 to 0.22) |
| Navigate example.com | 0.22 s | 0.14 s |
| Navigate Google search | 4.29 s | 0.75 s |
| Navigate GitHub sign-in | 0.98 s | 1.43 s |
| Navigate Walmart search | 7.11 s | 9.37 s |
| Navigate Target search | 5.40 s | 3.81 s |
| Snapshot | 0.01 to 0.12 s (2.09 s once, Target) | 0.04 to 0.13 s |
| Memory, about:blank after a refused load | 288 to 385 MiB | 20 MiB |
| Memory, example.com | 399 MiB | 23 MiB |
| Memory, GitHub sign-in | 450 MiB | 64 MiB |
| Memory, Walmart search | 695 MiB | 279 MiB |
| Memory, Target search | 894 MiB | 237 MiB |

This Mac (macOS 26.5, Apple M-series), unjailed, 10 runs each, against Playwright's headless shell (`chromium_headless`,
what `--browser=chromium` uses, which starts much faster than the server's headed Chrome):

| What | Chromium headless shell | Lightpanda |
|---|---|---|
| `open(None)` | median 0.13 s (0.10 to 0.49) | median 0.09 s (0.07 to 0.13) |
| Navigate the fixture actions page | 0.021 s | 0.003 s |
| Navigate example.com | 0.094 s | 0.061 s |
| Navigate github.com/login | 0.56 s | 1.03 s |
| Snapshot of the fixture page, example.com, GitHub | 6, 15, 41 ms | 3, 4, 16 ms |
| RSS after load: fixture page, example.com, GitHub | not measured: other Chrome processes ran on this Mac | 74, 77, 126 MiB |

So on the server Lightpanda starts about 12 times faster than Chrome (median) and uses 2.5 to 20 times less memory.
Light pages load faster; pages with heavy scripts (GitHub, Walmart) load slower, because Lightpanda runs every script
to completion before `load`.

### What the agent loses

| | Chromium | Lightpanda |
|---|---|---|
| Session export (sign-ins saved) | yes | yes, HttpOnly included |
| Screenshots | yes | no |
| Live view, and so hand-off to the user (sign-ins, CAPTCHAs, press-and-hold) | yes | no |
| Clicks at a point, the mouse, scrolling | yes | no; refs and selectors work |
| Downloads | yes | no |
| Infinite scroll and anything that waits for scrolling or visibility | yes | no: nothing scrolls |
| Passing as a normal browser | mostly | no: it says it is Lightpanda |

### Verdict

Not as the default. It cannot hand the browser to the user at all, which is the product's way out of every sign-in and
bot check, and it announces itself, so a site that blocks bots can refuse it on the user agent alone (Amazon did, from
the server). Making it the default would need pixels and layout upstream, which is not where Lightpanda is going.

It is worth keeping as a fast first pass. It is the only non-Chromium engine so far that exports a complete session,
so a run can start in Lightpanda (0.08 s, about 20 to 280 MiB), and when it needs a person, a screenshot, the mouse, a
download, or a site refuses it, `release()` the state and `open()` it in Chromium, then hand off there. That needs a
small switch in the browser service (an engine change on `NotSupported` or on a block page); it is not built here.
