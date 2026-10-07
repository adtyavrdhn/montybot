# The Camoufox backend

`CamoufoxBackend` in `camoufox.py` runs [Camoufox](https://github.com/daijro/camoufox) (MPL-2.0) behind the browser
contract (`contract.py`). Camoufox is Firefox with its fingerprint (user agent, platform, screen, WebGL, fonts, speech
voices and more) replaced in C++ from a config it reads at start. Issue #18, next to the Servo evaluation
(`servo.md`). Tested with Camoufox 156.0.1-beta.36 on macOS (Apple Silicon) and Ubuntu 24.04 (x86_64: the server, in
the app image, and CI's `camoufox-linux` job).

```python
from montybot.browser.camoufox import CamoufoxBackend, CamoufoxOptions

browser = CamoufoxBackend(CamoufoxOptions(headless=True))  # a Mac on macOS, a Windows desktop on Linux, en-US
await browser.open(state)
```

Get Camoufox from its [release page](https://github.com/daijro/camoufox/releases/tag/v156.0.1-beta.36). The release has
no checksum file; these are GitHub's asset digests, checked against our downloads:

| Asset | SHA-256 |
|---|---|
| `camoufox-156.0.1-beta.36-lin.x86_64.zip` | `72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53` |
| `camoufox-156.0.1-beta.36-lin.arm64.zip` | `8b5fd083721c807ee2a752265683482477105052fa0e21739388b527ae067c15` |
| `camoufox-156.0.1-beta.36-mac.arm64.zip` | `0ad5663e40f4a2a836e28747256b79f39a5105905f1b244139a7a391f85fda32` |

Each zip is about 1.3 GB (2.4 GB unpacked, most of it the font bundle that lets it pass as another OS). The backend
looks for the binary at `$MONTYBOT_CAMOUFOX_BINARY`, else `~/.cache/montybot/camoufox/Camoufox.app/Contents/MacOS/camoufox`
on macOS and `~/.cache/montybot/camoufox/camoufox` on Linux. Unzip with `unzip`, which keeps the files' modes.

## How it is driven: WebDriver BiDi

Three ways to drive the Camoufox binary were tried:

| Protocol | Works with Camoufox 156 | Start, unjailed headless on the server | Notes |
|---|---|---|---|
| Playwright's Juggler (`playwright.firefox`, `executable_path`) | yes, with our Playwright 1.63 | 3.3 s (3 runs) | The path Camoufox's own package uses. It pins `playwright<1.63`, because "every Playwright minor is free to change Juggler", so Camoufox would hold back the Playwright Chromium uses, or break on an upgrade |
| WebDriver BiDi (`--remote-debugging-port`) | yes | 3.4 s (3 runs) | Firefox's own remote agent, in the Camoufox build; `navigator.webdriver` stays false |
| Marionette | not tried | | Firefox's older protocol; BiDi replaces it |

BiDi was chosen: it is a W3C protocol maintained by Mozilla inside the same binary, needs no Node driver process, does
not couple Camoufox's version to Playwright's, and starts as fast. It also has what the contract needs: cookies for
every host with HttpOnly ones (`storage`), script calls in a sandbox the page cannot see (`script`), input that keeps
pressed buttons between calls (`input`), screenshots, and download events. What Juggler has and BiDi does not:
Camoufox's humanized cursor movement (`humanize`), which is implemented in its Juggler input code.

The remote agent's "recommended" prefs are switched off (`remote.prefs.recommended=false`): they are for test suites
(popups allowed, permissions in testing mode, a blank page instead of HTTP error pages) and are not what Camoufox's own
defaults were tuned with.

## Fingerprint

`camoufox_fingerprints/windows.json` and `macos.json` are Camoufox configs generated once with Camoufox's Python
package (0.5.8, `launch_options(os=..., locale='en-US', window=(1280, 800))`), with a few values fixed by hand: 8 cores,
no touch points, and the Windows screen's available area at the origin. They are passed in `CAMOU_CONFIG_1`, `_2`, ...,
with `window.outerWidth` and `outerHeight` set from `window_size`. `config=` overrides any key, such as `timezone`.

- **Linux (the server): a Windows 10 desktop**, Firefox 156, 1920x1080, Intel HD Graphics through ANGLE. The fonts are
  the bundle's Windows set, through a fontconfig file the backend writes into the profile. Without a font cache each
  start scans about a gigabyte of fonts, so the backend builds one with `fc-cache` outside the jail
  (`~/.cache/montybot/camoufox-fontconfig`) and mounts it read-only in the jail.
- **macOS: a Mac.** Any other identity makes Camoufox register its bundled fonts with macOS on every start: 24 to 27 s
  per start measured with the Windows identity, against 2.6 s with the Mac one.
- **en-US whatever the host says.** `intl.accept_languages` and the config's locale give `navigator.languages =
  ["en-US", "en"]` in the jail too, where `LANG` is `C.UTF-8` (Servo leaked `["C"]`).
- **WebRTC stays on**, as in any Firefox, with ICE limited to the proxy and no host candidates
  (`media.peerconnection.ice.proxy_only`, `no_host`, `default_address_only`). In the jail there is no UDP route anyway.

Every montybot browser on a platform wears the same identity, so a site could group them. A per-user identity, saved
with the user's sign-ins, would fix that.

## Conformance

All 16 tests of `BrowserBackendConformance` pass with `not_supported` empty, headless on macOS and in CI, and headed in
Xvfb inside the jail on the server and in CI (`TestJailedCamoufox`).

| Feature | How |
|---|---|
| `open` seeding | Cookies with `storage.setCookie`, for any host and without a page. localStorage on `ORIGIN/robots.txt` in a background tab. sessionStorage with a preload script in our sandbox, removed after the first page loads |
| `export`, `release` | `storage.getCookies` returns every cookie of the default partition, HttpOnly too. localStorage for the current origin, and in a background tab for each origin that was seeded or loaded in the tab. sessionStorage for the current origin |
| actions | `input.performActions`; selectors are found and scrolled into view by a script, then clicked at their centre; refs from `SnapshotWalker` |
| navigation | `browsingContext.navigate` waiting for `complete`; a failed load raises `ActionFailed` with Firefox's reason (`NS_ERROR_CONNECTION_REFUSED`, `dnsNotFound`, ...) and leaves Firefox's error page, whose URL is the one that failed |
| clicks and keys that navigate | navigation events (`navigationStarted`, then `load`, `navigationFailed`, a fragment or a download) |
| downloads (`DownloadsBackend`) | `browser.setDownloadBehavior` into the profile folder, `downloadWillBegin` and `downloadEnd` events |
| screenshots | `browsingContext.captureScreenshot` of the viewport |

Found while building it:

- **A cookie with SameSite=None must be Secure** in Firefox, so such a cookie without Secure is seeded with SameSite
  unset (`default`). A cookie a site set without SameSite comes back as `Lax`, as Playwright reports it for Chromium.
- **Partitioned cookies are not exported.** Firefox keeps third-party cookies in a partition per top-level site (Total
  Cookie Protection); `getCookies` without a partition reads the default one, which holds every first-party cookie,
  sign-ins included.
- **Start waits for Firefox's idle start-up tasks.** BiDi's `session.new` waits for `browser-idle-startup-tasks-finished`.
  On the server that is about 2.2 s of the jailed start; the process takes another 2.3 s to listen (below).

## Linux, in the server's jail

`CamoufoxOptions.server()`, or `BROWSER_BACKEND=montybot.engines:camoufox_server`, runs Camoufox headed in its own
Xvfb screen, inside Chromium's jail (`chromium_linux.bwrap_command`): its own user, pid, IPC, UTS and network
namespaces, host files read-only, only the profile folder writable, `--die-with-parent`, a cleared environment with
Camoufox's config added (`env=`) and the font cache mounted read-only (`read_only=`). Two socat processes are its only
connections to the host:

- **Out:** Firefox's SOCKS5 proxy prefs point every request at `127.0.0.1:1080`, loopback included
  (`network.proxy.allow_hijacking_localhost`, empty `no_proxies_on`), with DNS resolved by the proxy
  (`socks_remote_dns`), no direct fallback, and Firefox's own DNS over HTTPS off. socat forwards the port to the
  `EgressProxy`, which refuses non-public addresses. On the server `10.128.0.2` and `169.254.169.254` both failed with
  `NS_ERROR_CONNECTION_REFUSED`.
- **In:** BiDi's port is published only as `bidi.sock` in the profile folder (mode 600), so nothing listens on the
  host's network. `test_jailed_camoufox_reaches_no_private_address_and_no_host_port` checks both.

The app image has every library Camoufox needs (it is built on Playwright's image, which has Firefox's). It does not
ship Camoufox. CI's `camoufox-linux` job installs Firefox's libraries with `playwright install-deps firefox`.
`tests/linux/run.sh` is not set up for Camoufox; the jail was tested on the server's Docker and in CI.

## Engine evaluation (#18)

### End-to-end suite

`uv run --frozen pytest -q -p no:cacheprovider -n 4 tests/e2e --browser=camoufox` on macOS 26.5 (Apple M-series, 10
cores), scripted model, fixture sites, headless Camoufox with the Mac identity:

| | Chromium (`e2e-chromium` in CI) | Servo 0.7.0 | Camoufox 156.0.1-beta.36 |
|---|---|---|---|
| Passed | 94 of 94 | 83 of 94 | **94 of 94** (5 skipped, as for every engine: live and model-only tests), 187 s |
| Sign-in hand-off | passes | fails: no `autofocus`, Enter does not submit | passes |
| Sign-in saved for the next run | passes | fails: no HttpOnly export | passes |
| Press-and-hold check in the live view | passes | fails | passes |
| Downloads (`test_files`) | passes | fails | passes |
| Live view | CDP screencast | polled screenshots, about 20 fps | polled screenshots (`PollingFrameSource`), about 4 fps on the server |
| WebSockets on the server | through the proxy | none (fail closed) | through the proxy (SOCKS5) |

### Real sites from the server

On the GCP VM (x86_64, 4 cores, datacenter address), in a throwaway image `FROM montybot-app:latest`, both engines
jailed with their own egress proxy, a fresh browser per site: `open(None)`, `act(Navigate(url))`, 5 seconds, then
`snapshot()` and `screenshot()`. Chromium is `montybot.engines:chromium_server` (Playwright, headed in Xvfb); Camoufox is
`CamoufoxOptions.server()` (BiDi, headed in Xvfb, the Windows identity). One run each; the page texts were read for
blocks, not only searched for keywords.

| Site | Chromium | Camoufox |
|---|---|---|
| example.com | loads | loads |
| Google search | results | results |
| github.com/login | loads | loads |
| Walmart search | results | results |
| Amazon search | results | results |
| Target search | "We couldn't find a match for your search." | the same at 5 s; read again at 20 s, 3 tries each: Camoufox "228 results for eggs" twice, Chromium "couldn't find" three times |
| Best Buy | loads | loads |
| Instacart | loads | loads |
| LinkedIn sign-in | loads | loads |
| Reddit | blocked: "You've been blocked by network security." | blocked, the same page |
| nowsecure.nl | headings and the check's iframe, no result shown in 5 s or 20 s | the same |
| `10.128.0.2`, `169.254.169.254` | refused (`ERR_SOCKS_CONNECTION_FAILED`) | refused (`NS_ERROR_CONNECTION_REFUSED`) |

Seconds for `act(Navigate)` (to the load event, one run each), Chromium then Camoufox: Google 3.8 / 0.3, Walmart 6.4 /
3.9, Amazon 5.2 / 1.8, Target 15.4 / 2.0, Best Buy 5.6 / 3.8, Instacart 3.2 / 3.7, GitHub 1.0 / 0.7.

bot.sannysoft.com:

| Row | Chromium | Camoufox |
|---|---|---|
| WebDriver | missing (passed) | missing (passed) |
| Chrome | present (passed) | missing (marked failed): a Chrome-only check, and Camoufox says it is Firefox, which has no `window.chrome` |
| Plugins | 5 | 5 |
| Languages | `en-US,en` | `en-US,en` |
| WebGL vendor, renderer | `Google Inc. (Google)`, `ANGLE (... SwiftShader ...)`: a software renderer, a server tell | `Google Inc. (Intel)`, `ANGLE (Intel, Intel(R) HD Graphics Direct3D11 ...)`, from the identity |
| `PHANTOM_*` (UA, properties, ETSL, language, websocket, overflow, window height) | all ok | all ok |
| `HEADCHR_*` (UA, chrome object, permissions, plugins, iframe) | all ok | all ok |

The Servo run on the same VM, for comparison: CAPTCHA on Google, "The requested URL was rejected" on Walmart, Best Buy
did not load, and sannysoft failed plugins, WebGL vendor and `PHANTOM_ETSL`.

### Start and memory

| What | Chromium (Playwright) | Camoufox |
|---|---|---|
| `open(None)`, server, jailed, headed in Xvfb | 0.9 to 1.9 s | 4.4 to 7.0 s, mostly about 4.5 s |
| of which: process start until BiDi listens | | 2.3 s (2.0 headless) |
| of which: `session.new` (Firefox's idle start-up tasks) | | 2.2 s |
| `open(None)`, macOS, headless, 10 runs | | median 3.9 s (3.0 to 5.1) |
| Memory, about:blank, 8 s after start (PSS of the process tree, Xvfb and socat included) | 293 MiB | 466 MiB |
| Memory, 5 s after loading a real site (PSS, same tree) | 310 to 1042 MiB | 510 to 1197 MiB |
| Screenshots polled back to back, example.com, 1280x715 | 12 per second | 4 per second |

PSS splits each shared page between the processes that share it, so it compares multi-process browsers fairly. By RSS,
which counts shared libraries again in every process, Chromium is the larger one (1.1 to 2.5 GiB against 0.9 to
1.7 GiB).

### What the agent loses against Chromium

- **Nothing in the contract:** export with HttpOnly cookies, downloads, screenshots, the hand-off and the live view
  all pass the same tests.
- **Start time:** about 4.5 s against about 1 s, per browser.
- **The live view is slower:** about 4 polled PNG frames a second, one tab only, no phone layout
  (`set_viewport`) and no screen-reader outline, which are Chromium's screencast source's.
- **Export reads localStorage per origin** with a `robots.txt` request in a background tab, and misses partitioned
  third-party cookies.
- **No humanized cursor:** Camoufox's is in its Juggler code.
- **Size:** 2.4 GB unpacked, not in the app image.

### Verdict

Not the default yet. From the server's address Camoufox got everything Chromium got (Google, Walmart and Amazon results,
the same Reddit block) and did better on Target and on fingerprint consistency, where Chromium shows a software
renderer. It passes the whole contract and the whole e2e suite, which Servo did not. But Chromium is not being blocked
on these sites today, and Camoufox costs four times the start time, more memory per browser, a 2.4 GB bundle and a
slower live view.

To make it the default it would need: a per-user identity (generated, then saved with the user's sign-ins) and the
user's timezone; a faster live view (JPEG screenshots, or a screencast); a pool of started browsers to hide the start;
the bundle in the app image, pruned to the Windows fonts; a check of how much of Firefox's own sandbox works inside
bwrap; and a longer run of real tasks (signed-in carts, repeated daily runs) showing Chromium being challenged where
Camoufox is not. Until then it is a good second engine for a site that blocks Chromium.
