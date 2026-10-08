# CloakBrowser under the CDP backend

CloakBrowser (github.com/CloakHQ/CloakBrowser) is a Chromium with about 87 fingerprint patches in its C++ source:
canvas, WebGL, audio, fonts, GPU, screen, WebRTC, automation signals. `cloak.py` runs its binary in place of Chrome
under `ChromiumCDPBackend` (`cdp.py`, evaluated in `cdp.md`): the same CDP pipe, the same bwrap jail, the same
`EgressProxy`. No Playwright, and not their Python wrapper. Engine evaluation #18, next to `cdp.md`, `servo.md` and
`lightpanda.md`.

Why: Nordstrom shows the production browser "We've noticed some unusual activity ... we don't allow unidentified,
automated traffic". CloakBrowser gets through (below).

```python
from sammy.browser.cdp import CDPOptions, ChromiumCDPBackend
from sammy.browser.cloak import with_cloak

browser = ChromiumCDPBackend(with_cloak(CDPOptions.server()))  # the Linux server: headed on Xvfb, in bwrap
```

`BROWSER_BACKEND=sammy.engines:cloak_server` runs it in the app, `cloak_headless` unjailed for tests, and
`--browser=cloak` in the end-to-end suite. The default stays `chromium_cdp_server`. Read the license section before
turning it on for users.

## How it is launched

| Part | How |
|---|---|
| Binary | `$SAMMY_CLOAK_BINARY`. Never in the repository or a published image. `tests/linux/fetch_cloak.sh` downloads the free build from GitHub Releases, checks its pinned SHA-256, verifies CloakHQ's Ed25519 signature on the release's `SHA256SUMS` (their wrapper's key) and that the signed manifest names the same version and hash |
| Build | linux-x64 146.0.7680.177.5 (CI, the VM); darwin-arm64 145.0.7632.109.2 (this Mac). Both free, no key, no sign-in |
| Jail, pipe, proxy, Xvfb | unchanged: `with_cloak` only swaps `executable` and adds flags, so `bwrap_command` binds the binary's folder read-only as it does Chrome's |
| Our launch flags | all kept (no automation flags, `--disable-blink-features=AutomationControlled`, the background-networking switches, the isolated world, Runtime never enabled) except `--enable-unsafe-swiftshader`, which their wrapper drops too: its renderer string is a tell, and CloakBrowser reports a GPU of its own (`CDPOptions.software_webgl=False`) |
| Their flags we add | `--fingerprint=SEED`, `--fingerprint-platform`, `--fingerprint-timezone=America/Chicago`, `--lang=en-US`, `--accept-lang=en-US,en`, and headed `--ignore-gpu-blocklist` (their wrapper's, so WebGL runs on Xvfb) |
| Their flags we leave out | `--no-sandbox` (their wrapper passes it; Chrome's own sandbox works inside bwrap, as for Chrome), `--fingerprint-locale` (it cut `navigator.languages` to `["en-US"]`; a stock en-US Chrome has `["en-US", "en"]`), `--fingerprint-storage-quota` (the binary normalizes the quota when a seed is set), `--start-maximized` (their wrapper adds it only on 148+) |

Checked for conflicts: we set no user agent (the binary makes one per platform); `--lang` was not set before;
`--disable-blink-features=AutomationControlled` is harmless next to their patches (`navigator.webdriver` is false
either way); `--disable-features=Translate,MediaRouter,...` matches what their wrapper passes through Playwright on
146. A unit test checks that no flag is given twice.

### Which device it presents

`Fingerprint` holds the seed, platform, time zone and locale; the binary derives GPU, screen, memory and the canvas,
audio and client-rect noise from the seed.

- **Seed: one per deployment, stable.** From `$SAMMY_CLOAK_SEED` (a number, or any text, hashed into their
  wrapper's 10000 to 99999 range), else from `sammy`. The same seed is the same device on every launch, so a
  returning user (their cookies come back from the saved state) also comes back on the same device. Their wrapper
  picks a random seed per launch; that would show a signed-in user a "new device" on every run.
- **Not per user, yet.** The backend does not know the user: `BrowserHost.start` has the user id but calls a
  factory with no arguments. A seed per user is a small change there (pass `user_id` to the factory, and
  `fingerprint_seed(f'{deployment}:{user_id}')`); it is left out to keep this change away from the host, which #88
  reworks. Until then every user of a deployment shares one device, from the one server address anyway.
- **Platform:** `windows` on Linux, as their wrapper does, `macos` on a Mac. A Windows device on a Linux server has
  Linux fonts only (CreepJS found 1 of its 51 test fonts); their README asks for Windows fonts copied from a Windows
  machine, which we cannot ship. The Linux persona was tried too: no better (below).
- **Time zone and locale:** America/Chicago (the server's address is GCP us-central1, Iowa), en-US.

## Conformance

All 16 tests of `BrowserBackendConformance` pass, export included:

| Where | Result |
|---|---|
| macOS (darwin-arm64 145), headless | 16/16 |
| Linux CI (`cloak-linux`), headless | 16/16 |
| Linux CI, in the server's jail: headed on Xvfb, bwrap, egress proxy | 16/16 |

`tests/browser/test_cloak.py` also checks, jailed, that loopback, private and metadata addresses are refused and no
TCP port opens on the host; that a page sees `navigator.webdriver` false, no "Headless", `["en-US", "en"]`,
America/Chicago and the persona's platform (headless and jailed); and the command line without a binary. The live
view engine tests run on `cloak` too: 7 of 7 on this Mac and in CI.

## Engine evaluation

### (a) End-to-end suite

`SAMMY_CLOAK_BINARY=... uv run --frozen pytest -q -p no:cacheprovider -n 4 tests/e2e --browser=cloak` on this Mac
(darwin-arm64 build 145, headless):

| | CDP Chrome (`--browser=cdp`) | CloakBrowser (`--browser=cloak`) |
|---|---|---|
| Passed | 94 of 94 (98 s, same day) | 94 of 94 (69 s; the first run, with a cold binary, 241 s) |
| Sign-in hand-off, saved sign-in, press-and-hold, downloads | pass | pass |

CI runs the same suite on Linux with the linux-x64 build (`cloak-linux`): 95 passed, 4 skipped (Linux runs one test
more than macOS), 102 s.

### (b) Real sites from the server

On the GCP VM (datacenter address), the method of `cdp.md`: a throwaway image `FROM sammy-app:latest` with the
README's baseline fonts and the binary downloaded and verified in the build, this branch's `sammy/` mounted, both
engines jailed and headed on Xvfb, each with its own egress proxy, a fresh browser per site, `open(None)`, `Navigate`
(60 s), 5 s, then the page read and a screenshot. Every page text was read. Times are open + navigate, seconds.
"CDP" is `chromium_cdp_server`, production (Chrome for Testing 153).

| Site | CDP (production) | CloakBrowser 146 (Windows persona) |
|---|---|---|
| example.com | loads (1.1 + 0.3) | loads (0.9 + 0.3) |
| Google search | results (1.0 + 4.2) | results (0.9 + 4.0) |
| github.com/login | loads (1.0 + 1.0) | loads (0.9 + 0.9) |
| Walmart search, 3 runs | results 3 of 3 (1.0 + 6.0 to 6.3) | results 3 of 3 (0.8 + 8.8 to 9.0) |
| Amazon search, 3 runs | results 3 of 3 (0.9 + 3.3 to 3.5) | **"Sorry! Something went wrong!" 3 of 3** (0.9 + 0.4) |
| Target search, 3 runs | results 3 of 3 (0.9 + 4.3 to 11.0) | results 3 of 3 (0.8 + 6.5 to 6.8) |
| Best Buy | loads (1.0 + 5.0) | loads (0.9 + 6.6) |
| Instacart | loads (1.0 + 2.2) | loads (0.8 + 2.8) |
| **Nordstrom home, 3 runs** | **"We've noticed some unusual activity" 3 of 3** (redirect to siteclosed.nordstrom.com) | **loads 3 of 3** (0.8 + 0.5) |
| **Nordstrom search "socks", 3 runs** | **blocked 2 of 3**, results once | **results 3 of 3** ("You searched for socks", 3206 items) |
| Reddit | "You've been blocked by network security" (the address) | same |
| LinkedIn sign-in | loads | loads |
| nowsecure.nl | the page and its check iframe, nothing more in the text: inconclusive | same |
| 10.128.0.2, 169.254.169.254 | refused (`ERR_SOCKS_CONNECTION_FAILED`) | refused (same) |

Amazon refuses CloakBrowser on the first response, 0.4 s in, before any script could run: the request itself, not a
script's fingerprint. Same address, same headers but these: `sec-ch-ua` says Google Chrome 146 on Windows (Chrome for
Testing says Chromium 153 on Linux), and the TLS hello differs (JA4 `t13d1516h2_8daaf6152771_d8a2da3f94cd` against
`t13d1517h2_8daaf6152771_cb7bf5808d99`); HTTP/2 settings are the same. A five-month-old Chrome claiming Windows from a
Linux datacenter host is a plausible reason; we could not tell which signal Amazon uses.

The Linux persona (`Fingerprint(platform='linux')`, one run each) was no better: Amazon refused the same way, and
Nordstrom's search was blocked once; Walmart, Target and Nordstrom's home page loaded.

### (c) Bot-detection pages

bot.sannysoft.com, from the VM, jailed and headed:

| Row | CDP (production) | CloakBrowser |
|---|---|---|
| User Agent | `X11; Linux x86_64 ... Chrome/153` | `Windows NT 10.0; Win64; x64 ... Chrome/146` |
| WebDriver | missing (passed) | missing (passed) |
| WebDriver Advanced | passed | passed |
| Chrome | present (passed) | present (passed) |
| Plugins Length | 5 | 5 |
| Languages | en-US,en | en-US,en |
| WebGL Vendor | Google Inc. (Google) | Google Inc. (NVIDIA) |
| WebGL Renderer | ANGLE ... **SwiftShader** driver | ANGLE (NVIDIA, NVIDIA GeForce RTX 5090 ... Direct3D11) |
| `PHANTOM_*`, `HEADCHR_*`, `SELENIUM_DRIVER`, `CHR_*` | all ok | all ok |
| Screen | 1280 x 800 (the Xvfb screen) | 1920 x 1080, available 1920 x 1032 (a taskbar) |

| Page | CDP (production) | CloakBrowser |
|---|---|---|
| CreepJS "like headless" | 50% | 25% |
| CreepJS headless, stealth | 0%, 0% | 0%, 0% |
| CreepJS time zone, fonts | UTC; 2 of 51 fonts | America/Chicago; 1 of 51 fonts |
| BrowserScan bot detection | Normal on every row, CDP included | Normal on every row, CDP included |

CreepJS gives no single score any more. The RTX 5090 comes from the seed; with a Linux CPU and Linux fonts behind it,
a careful site can still see a mismatch. The page was read about 3 s after load.

### (d) Speed and memory

Start (`open(None)`) and the RSS of every process of the browser (`bench_chromium.usage`), medians:

| | Server, CDP | Server, CloakBrowser | Mac headless, CDP | Mac headless, CloakBrowser | Mac headed, CDP | Mac headed, CloakBrowser |
|---|---|---|---|---|---|---|
| start, s | 0.92 | 0.89 | 0.32 | 0.22 | 0.31 | 0.46 |
| RSS on about:blank, MiB | 1063 | 945 | | | | |
| RSS on the fixture site, MiB | | | 914 | 653 | 930 | 667 |
| RSS on example.com, MiB | 1116 | 1013 | | | | |
| RSS on Target search, MiB | 2117 to 2126 | 1895 to 1964 | | | | |

Server: the VM, jailed and headed on Xvfb, 5 starts each; opening a browser on the real sites took 0.81 to 0.98 s
for CloakBrowser and 0.91 to 1.41 s for CDP. Mac: 10 starts each. CloakBrowser is built on ungoogled-chromium and
starts as fast. Where both engines got the same page, its processes used 7 to 17 % less RSS (Target: 48 processes
against 79); the Mac build, 145, about 28 % less. RSS counts shared pages once per process, so it overstates a single
browser, for both.

### (e) What the agent loses

Nothing in the contract: it is the same backend.

| | CDP | CloakBrowser |
|---|---|---|
| Export, `release` (HttpOnly cookies) | yes | yes (conformance) |
| Downloads (#21) | yes | yes (e2e) |
| Live view (#14), tabs, phone size, outline | yes | yes (7 of 7 engine tests) |
| Screenshots | yes | yes |
| Hand-off and saved sign-ins | yes | yes (e2e) |

What changes: the Chromium version is CloakHQ's, behind Chrome (146 free on Linux, 145 on Mac, against 153), and
newer builds need a paid subscription. Security fixes in Chromium reach us only when CloakHQ publishes a free build.

### Does the binary call home?

Not in what we saw. The license allows "operational communications required for license validation and monitoring
the number of concurrently open sessions".

- **Jailed** (every connection through the `EgressProxy`, which logged each host): across all the runs above,
  CloakBrowser connected only to the sites and their resources. No `cloakbrowser.dev`, no CloakHQ host. Production
  Chrome, with the same "no background networking" flags, also opened `update.googleapis.com`,
  `clients2.google.com` and `www.google.com` on every one of its 28 runs, and `android.clients.google.com` on 23.
- **Unjailed** (its own network in a container, `tcpdump` on DNS and every new connection, 120 s idle on
  about:blank, then example.com and 30 s more): CloakBrowser looked up and connected to `example.com` only. Chrome
  looked up `accounts.google.com`, `android.clients.google.com`, `clients2.google.com`, `mtalk.google.com`,
  `update.googleapis.com` and `www.google.com` while idle.

So the free build, with no license key, does not phone home in these runs. Their README says license and session calls
go directly, not through the proxy (`--license-through-proxy` on 148+), so in the jail such a call could not leave at
all; the unjailed run is the one that shows it was not attempted. Pro builds with a key were not tested.

## (f) License

- **Wrapper:** MIT. We use none of its code, only read it for the flags and the download check.
- **Binary:** proprietary, `BINARY-LICENSE.md` v1.3 in their repository. Free builds from GitHub Releases are allowed
  for "personal, commercial, and internal business purposes"; the latest major (154, "-pro" tags) needs a paid
  subscription. We used only the free builds, with no license key, no sign-in and no `cloakbrowser login`.
- **Redistribution is not allowed.** The binary is never committed or baked into an image we publish. CI and the VM
  download it from GitHub Releases, pinned by version and SHA-256, signature checked.
- **Our evaluation is internal use**, which the license allows.
- **Production for users likely needs a separate OEM/SaaS license.** The license requires one "if third-party
  customers are given technical access to, or control over, the browser capability itself", where control includes
  the ability "to operate ... or influence the CloakBrowser Binary, its browsing sessions". Sammy's hand-off gives
  the user the live browser, and users' agents drive it with their own code. Mike decides; contact is
  info@cloakbrowser.dev.
- **Acceptable use** forbids, among others, "automated account creation" and "circumventing authentication on systems
  you do not own". Signing in to a user's own account through the hand-off is not that, but worth knowing.

## Verdict

Not as the default today, and not for users without a license answer.

- **For:** it fixes the headline problem. Nordstrom let it through 6 of 6 times where production was blocked 5 of 6.
  It passes everything the CDP backend passes (conformance, live view, 94 of 94 end to end), starts as fast, uses less
  memory, removes the SwiftShader renderer and the "like headless" signals, and does not call home.
- **Against:** Amazon, which production handles, refused it 3 of 3 times on the first request. The free build trails
  Chrome by five months, with security fixes on CloakHQ's schedule. Users controlling it likely needs a paid
  OEM/SaaS license.

What it would take:

1. **The license:** ask CloakHQ whether sammy's hand-off needs the OEM/SaaS license, and its price. Without it, at
   most use it for runs no user drives.
2. **Per-site engine choice** rather than a swap: CloakBrowser for sites that block Chrome (Nordstrom), Chrome for the
   rest (Amazon). This needs the host to pick a factory per site or per user, which it cannot yet.
3. **A seed per user:** pass the user id to the backend factory (see above).
4. **Find Amazon's reason** before any wider use: try a newer free build when one appears, and the Linux persona with
   a real Linux GPU string, and compare TLS hellos.
5. **Soak it** next to `chromium_cdp_server` on real runs for a week, as `cdp.md` proposes for CDP.

## Not verified

- The jail on this Mac (Podman cannot remount `/etc/hosts` for bwrap); it ran in CI and on the VM.
- Headed on macOS beyond the start-time and memory runs.
- Linux on arm64 (a free build exists at 146.0.7680.177.4; not tried).
- Windows fonts: we have none to install, so the Windows persona's font list is a Linux one.
- Pro builds and anything that needs a key. Whether a future free build calls home.
- Why Amazon refuses it; which of the request differences matters.
- nowsecure.nl's result could not be read from the page text, for either engine.
