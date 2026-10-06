# Servo over WebDriver: second evaluation

The first Servo spike (`../servo`) embedded the `servo` 0.7.0 crate, read the page through AccessKit and clicked
through AccessKit actions. Critics called that unfair to Servo. This spike drives Servo the way Mike's hand-off PoC
(`../../poc`) proposes instead:

- servoshell with its built-in W3C WebDriver server, driven over plain HTTP from Python
- the agent's view built by injected JavaScript, with no AccessKit
- input through WebDriver Element Click, Element Send Keys and the Actions API
- the prefs real sites need turned on, and a desktop Chrome or Firefox user agent instead of one that says Servo
- the PoC's engine-neutral `BrowserState` filled and applied over WebDriver

It ran on the same M-series Mac (macOS 26.5, arm64) as `../engines` on 2026-10-06.

## Builds

Nothing was installed system-wide. Both builds are official servoshell macOS arm64 DMGs. Each DMG was mounted with
`hdiutil attach -nobrowse -mountpoint bin/mnt-*`, and `Servo.app` was copied into `bin/` (gitignored, 756 MB including
the DMGs).

| Build | `servoshell --version` | Source | sha256 of DMG |
|---|---|---|---|
| **nightly** (used for everything unless noted) | `Servo 0.7.0-648de26fa` (commit of 2026-10-05 20:57 UTC) | `gh release download 2026-10-05 -R servo/servo-nightly-builds` | `7dd6544c…f921` (matches the published .sha256) |
| v0.7.0 release | `Servo 0.7.0-aac43a3f3` (branch cut 2026-10-01) | `gh release download v0.7.0 -R servo/servo` | `1f9b5b6e…3014` (matches) |

The nightly is 77 commits ahead of v0.7.0, which is the code behind the 0.7.0 crate. None of those commits touch
accessibility role mapping or WebDriver cookies. The webdriver ones are #48566 and #48585, both about action
durations. v0.7.0 and the nightly behaved the same on example.com, github.com and the login checks.

## How servoshell is driven

```sh
servoshell --headless --webdriver=PORT --temporary-storage --window-size=1280x800 \
  --enable-experimental-web-platform-features \
  --pref=dom_crypto_subtle_enabled --pref=dom_intersection_observer_enabled --pref=dom_resize_observer_enabled \
  --pref=dom_adoptedstylesheet_enabled --pref=dom_fontface_enabled --pref=dom_indexeddb_enabled \
  --pref=dom_web_animations_enabled --pref=dom_permissions_enabled --pref=dom_storage_manager_api_enabled \
  --pref=dom_cookiestore_enabled \
  --user-agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36' \
  about:blank
```

The pref names come from `servo-config` 0.7.0 `prefs.rs`. The Firefox user agent was
`Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0`. servoshell's own default is
`… rv:153.0) Servo/0.7.0 Firefox/153.0`.

Some quirks of the WebDriver server, all measured:

- It only accepts `Host: 127.0.0.1:PORT`. With `localhost` you get `Invalid Host header localhost:7101`.
- POSTs need `Content-Type: application/json`, otherwise `Invalid Content-Type`.
- `--headless` works. Session capabilities report `browserName: servo`, `browserVersion: 0.0.1`, `platformName: mac`.
- A headless servoshell **keeps running after Delete Session and ignores SIGTERM and SIGINT**. It only goes away
  with SIGKILL. A supervisor has to kill it.
- `--host-file` maps hostnames to IPs. The cookie tests use it to put `www.shop.test`, `api.shop.test` and
  `shop.test` on 127.0.0.1.

## Run

```sh
cd spikes/servo-webdriver
python3 scripts/server.py 8767 &      # login page, /set, /setall, /check, /events
cd scripts
R="uv run --no-project --with psutil python"
$R measure.py example chrome 1        # SITE in example|github|walmart|walmart-search, UA in chrome|firefox|servo
$R measure.py walmart chrome 3 --hold # --hold: on a challenge, press and hold #px-captcha for 12 s via the Actions API
$R measure.py github chrome x --no-prefs --build=v070
$R checks.py form github state live density
$R live_concurrent.py
$R apis.py
python3 summarize.py
PLAYWRIGHT_BROWSERS_PATH=$PWD/../../engines/bin/ms-playwright uv run --no-project --with playwright==1.63.0 python chromium_control.py
```

## Method

**Timed runs** (`measure.py`, same shape as `../engines/scripts/measure.py --phase runs`). Each run spawns a fresh
servoshell and creates a session. It then calls Navigate To with `pageLoadStrategy: normal`, which blocks until the
load event, with a 60 s page-load timeout. It waits 2 s, then reads the title, URL and page source, runs the DOM
snapshot and a fingerprint probe, saves `out/<build>-<site>-<ua>-<run>.png`, and runs macOS `footprint` on the
process tree.

- **launch s**: from spawning the process to a created session (WebDriver `/status` ready, then New Session).
- **load s**: Navigate To round trip.
- **fp peak**: `footprint` → `phys_footprint_peak`, summed over the tree. This is the same function and column as
  `../engines` "fp". servoshell runs as one process unless you pass `-M`, so here it's one pid.
- **peak RSS**: psutil, sampled every 50 ms. It's an upper bound, as in `../engines`.
- **challenge**: "Robot or human" in the title or DOM, `px-captcha` in the DOM, or `/blocked` in the URL. These are
  the same rules as `../engines`, and every hit was checked against its screenshot.

**Politeness budget**, the same as the Chromium run. Walmart homepage: 3 loads (Chrome UA, Firefox UA, Chrome UA).
Walmart search: 2 loads (Chrome UA, Firefox UA). I added **one Chromium headless shell control load of walmart
search** (`chromium_control.py`), from the same egress about 10 minutes after the Servo loads. The egress looked
different from the `../engines` run (see the caveats), and without a control the comparison wouldn't be fair. That
control is the only walmart load beyond the budget.

**Agent snapshot** (`scripts/snapshot.js`). Execute Script walks the DOM, including open shadow roots. It keeps
visible interactive elements (implicit and ARIA roles, `onclick`, `tabindex`, `contenteditable`) plus headings. For
each one it computes the role, an accessible name (`aria-labelledby` → `aria-label` → `<label>` → button value or alt
→ text → placeholder or title), the current value (passwords masked), and states (checked, disabled, required,
expanded). It tags each element with a `data-mb-ref="eN"` that the agent passes back to Find Element. This is the
browser-use style.

**Caveats.**

- Walmart's UI put this egress in **Mississauga, Canada**, and the browser time zone was America/Toronto. The
  `../engines` run saw Sacramento, so the network conditions weren't identical.
- n is tiny: 3 homepage loads and 2 search loads.
- The loads ran back to back from one consumer IP that had already loaded walmart many times today in the earlier
  spikes (7 in `../servo`, 4-5 per engine in `../engines`).
- Bot results are anecdotes, not rates.

## Results (measured)

### Servo over WebDriver, per run group

Medians. Every run is in `out/results.jsonl`. All rows are the nightly with site prefs on, unless the row says
otherwise.

| site | UA | n | launch s | load s | fp peak MiB | peak RSS MiB | challenged | JS errors in log |
|---|---|---|---|---|---|---|---|---|
| example.com | Chrome | 3 | 0.070 | 0.089 | 348 | 138 | 0/3 | 0 |
| example.com | Firefox / Servo default | 1 / 1 | 0.070 / 0.081 | 0.078 / 0.092 | 347 / 348 | 138 | 0/2 | 0 |
| github.com/login | Chrome | 3 | 0.083 | 0.58 (0.51-0.73) | 457 | 239 | 0/3 | 0 |
| github.com/login | Firefox | 1 | 0.070 | 0.46 | 457 | 246 | 0/1 | 0 |
| github.com/login, **default prefs** | Servo default / Chrome | 1 / 1 | 0.08 | 0.97 / 0.86 | 450 | 229 | 0/2 | 4 each: `e.adoptedStyleSheets is undefined`, `IntersectionObserver is not defined` |
| walmart.com | Chrome | 2 | 0.073 | 1.72 (1.34-2.09) | 774 (758-790) | 534 | **0/2** | Next.js framework errors, a CSP-blocked script, `Failed to parse font face {}` |
| walmart.com | Firefox | 1 | 0.079 | 1.44 | 1236 | 993 | **1/1** (press & hold modal) | same, plus PerimeterX trying `chrome://juggler/content` |
| walmart.com/search?q=milk | Chrome | 1 | 0.082 | 1.95 | 697 | 412 | **0/1**: real search page, title `milk - Walmart.com` | CSP-blocked script, font face |
| walmart.com/search?q=milk | Firefox | 1 | 0.071 | 0.53 | 1636 | 1288 | **1/1** (`/blocked`, press & hold) | `chrome://juggler/content` |
| example / github (v0.7.0 release) | Chrome | 1 / 1 | 0.18 / 0.082 | 0.004 (anomalous) / 0.65 | 350 / 457 | 138 / 238 | 0/2 | 0 |

### Next to Chromium headless shell and the first Servo spike

The Chromium and first-spike rows are copied from `../engines/README.md`. "fp" is peak footprint in MiB, the same
`footprint` method everywhere.

| Engine | launch s | load s: example / github / walmart | fp MiB: login / example / github / walmart | walmart home challenged | walmart search | agent view: roles / names / typed value | BrowserState round trip | live view + input | users per process | memory per extra user |
|---|---|---|---|---|---|---|---|---|---|---|
| Chromium headless shell (Playwright, stock `HeadlessChrome` UA, `webdriver=true`) | 0.061 | 0.30 / 0.65 / 2.38 | 50 / 64 / 123 / 314 | 0/3 | blocked in `../engines`; **blocked again in today's control load** | yes / yes / yes (`aria_snapshot`) | yes, HttpOnly both ways | CDP screencast + `Input.dispatch*` | many isolated contexts | 20 MiB per context |
| Servo 0.7.0 crate, embedded (first spike) | n/a, in-process | 0.35-0.49 / 0.77-1.12 / 1.4-2.2 (incl. startup) | 308 / 308 / 399 / 505-674 | 6/7 | not tested | **no** (AccessKit had no roles, names or values) | via `site_data_manager` (HttpOnly ok), no BrowserState adapter | screenshot API + `notify_input_event` | 1 Servo per process | n/a |
| **Servo nightly over WebDriver (this spike)** | 0.070-0.083 | 0.089 / 0.58 / 1.72 | 339 / 348 / 457 / 774 (Chrome UA) | **Chrome UA 0/2, Firefox UA 1/1** | **Chrome UA: not blocked**; Firefox UA: blocked | **yes / yes / yes** (JS snapshot) | **yes except HttpOnly export**: HttpOnly import works, export doesn't | Take Screenshot polling 53-55 fps idle, 22 fps while acting; Actions ~22 ms per event | **1 session per process** (separate windows share the cookie jar) | ~131 MiB settled, ~335 MiB peak per extra process |

### Requirement checks

**Agent reads and acts on forms** (`out/checks-form.json`, `out/checks-github.json`).

- Local login page, before typing:

  ```
  heading "Sign in" [level=1]
  textbox "Username" [ref=e2]
  textbox "Password" [ref=e3]
  button "Log in" [ref=e4]
  link "A link" [ref=e5] href=https://example.com/
  ```

- After Find Element `[data-mb-ref="e2"]`, Element Click and Element Send Keys, the snapshot shows
  `textbox "Username" [ref=e2] value="alice"` and `textbox "Password" [ref=e3] value="*******"`. Clicking `e4`
  submits the form, and the page shows `submitted as alice`. Typing with Actions API `keyDown`/`keyUp` also works
  (value `bob`). The snapshot takes 3 ms.
- github.com/login gives 13 entries, including `textbox "Username or email address" [ref=e3] required`,
  `textbox "Password" [ref=e4] required` and `button "Sign in" [ref=e6]`. Typed values show up after filling. The form
  was not submitted.
- GitHub's "Sign in with a passkey" button doesn't appear, because Servo has no `PublicKeyCredential` (WebAuthn).
- walmart.com gives 149-151 entries (search combobox, departments, carousels, cookie banner). Walmart search gives
  54 entries, including `combobox "Search" [ref=e5] value="milk"`.
- WebDriver's own accessibility endpoints aren't usable yet. Get Computed Role returns `null` for `<input>` and
  `<button>`, because it only returns an explicit ARIA `role` attribute (servo issue #43734). Get Computed Label
  returns `500 unsupported operation: Command not implemented: GetComputedLabel(...)`.
- The AccessKit tree can't be reached from headless servoshell. `-Z accessibility-tree` with
  `--pref=accessibility_enabled` logged nothing, and the nightly has no role-mapping change (issue #46241 is still
  open). The first spike's finding stands, but the JS snapshot makes it irrelevant for the agent.

**Bot checks and compatibility** (`out/results.jsonl`, `out/apis.json`, screenshots in `out/`).

- With site prefs, example.com, github.com and walmart render correctly (`out/nightly-*-chrome-*.png`) and the logs
  show no JS errors for github.
- With default prefs, github still loads and the form is usable, but it throws on `adoptedStyleSheets` and
  `IntersectionObserver`. `crypto.subtle` exists by default in servoshell. The first spike had to turn on a crate
  feature for it.
- With a Chrome UA, walmart's homepage loaded clean 2/2 and the search URL loaded a real search page 1/1. On that
  search page Walmart added `&facet=fulfillment_method:Shipping` for this Canadian egress and showed "We couldn't find
  a match", with related searches and filter UI, but no products. So it got past PerimeterX, but I couldn't show
  product results.
- The Chromium headless shell control loaded the same search URL from the same egress about 10 minutes later and
  was sent to `/blocked` (`out/control-chromium-walmart-search.png`).
- With a Firefox UA, walmart challenged both loads (homepage modal, search `/blocked`). On those loads PerimeterX
  started a worker from `chrome://juggler/content`, which is Playwright-Firefox's protocol, and Servo logged
  `error loading script chrome://juggler/content (Blocked as mixed content)`.
- **Press and hold over the Actions API reaches the challenge.** The hold was pointerMove, pointerDown, a 12 s
  pause, then pointerUp, on `#px-captcha` (a cross-origin iframe). The widget reacted with "Please try again"
  (`out/nightly-walmart-search-firefox-2-after-hold.png`), so it rejected the attempt but did receive the input.
- Identity as a page sees it (Chrome UA, site prefs):
  - `navigator.webdriver` is `undefined`. Real Chrome and Firefox report `false`, and Playwright-driven browsers
    `true`.
  - `navigator.vendor` is `""`. Chrome reports `Google Inc.`.
  - No `navigator.userAgentData`, no `window.chrome`, and `plugins.length` is 0.
  - `platform` is `Mac`, not `MacIntel`.
  - `screen` is 2560x1600 at DPR 1 while the window is 1280x800.
  - Missing APIs: `PublicKeyCredential`, `navigator.credentials`, `serviceWorker`, `RTCPeerConnection`,
    `PaymentRequest`.

**BrowserState round trip** (`scripts/servo_backend.py`, `out/checks-state.json`, `out/servo-browser-state.json`).

- **Get All Cookies does not return HttpOnly cookies.** The server set `sid` HttpOnly and still received it, but
  WebDriver listed only `pref`, `plain` and `jsc`. Get Named Cookie `sid` returns `404 no such cookie`. The cause is in
  `components/script/event_loop/webdriver_handlers.rs`: `handle_get_cookies` and `handle_get_cookie` query the jar
  with `CookieSource::NonHTTP`. The spec's cookie object carries an `httpOnly` field, and chromedriver and
  geckodriver return HttpOnly cookies (known behaviour, not re-measured here).
- **Add Cookie with `httpOnly: true` works.** The server then receives the cookie.
- Add Cookie only accepts a `domain` equal to the current document's host. From `www.shop.test`, `.shop.test` and
  `shop.test` both give `400 invalid cookie domain`, and so does `about:blank`. The workaround in `servo_backend.py`
  works: load a document on `shop.test` itself (`/robots.txt`) and add with `domain: "shop.test"`. The cookie then
  reaches `api.shop.test`.
- Get All Cookies only covers the current document. Export visits each known origin's `/robots.txt` in a side tab,
  which shares the cookie jar, so the handed-over tab keeps its page. You must know the origin list.
- Domain cookies come back as `domain: "shop.test"` and host-only cookies as the document host. `sameSite` isn't
  returned (defaulted to `Lax`).
- localStorage per origin and sessionStorage for the tab work through Execute Script. There's no init-script hook,
  so sessionStorage is written on a same-origin `/robots.txt` in the same tab before navigating. The page then sees
  `views=2` on first load.
- **Result.** Export with A and import with B carried `pref` (domain cookie, seen on www and api), `plain`, `jsc`,
  both origins' localStorage and the sessionStorage, but **not** `sid`. Applying the same state plus `sid`
  (HttpOnly), as the Playwright side would export it, delivered all four cookies to the server.

**Live view and human takeover** (`out/checks-live.json`, `out/checks-live-concurrent.json`).

- Take Screenshot (full-viewport PNG, 1280x800):
  - login page: median 17.2 ms (p90 23 ms), 31 KB, 55 fps polled back to back
  - github: 18.4 ms, 53 KB, 53 fps
  - frames change while typing
- There's no push or screencast and no damage events. Frames must be polled.
- Polling screenshots in one thread while sending Actions in another, on the same session: 22.5 fps, with actions
  at a median of 52 ms (p90 64 ms). All 20 typed characters arrived.
- Replaying a recorded human sequence (moves, down, a 900 ms pause, a move, up) as one Actions call gives trusted
  `pointerdown`, `mousedown`, `pointerup` (`held: 1511` ms), `mouseup` and `click` events.
- Sending each event as its own call, the way a live relay would, also works:
  - pointerMove: 22 ms per call
  - pointerDown: 22 ms
  - pointerUp 1 s later: gives `held: 1005`
  - key: 42 ms
- In an earlier probe, a 10 px move between down and up fired no `click`. A 5 px move did fire one.

**Multiple users** (`out/checks-density.json`).

- A second New Session on the same process returns
  `500 session not created: Session is already started`, and `/status` turns to `ready: false`.
- New Window inside the session shares the cookie jar: a second window sees `sid`. So it's **one process per user**.
- Separate processes are isolated: user 1 doesn't see user 0's cookie.
- Five processes on the login page: the first was 144 MiB settled (429 MiB peak), and each extra one was **131 MiB
  settled and 335 MiB peak**.
- An idle servoshell (about:blank, session open) is 82 MiB settled but already 285 MiB at peak. About 200 MiB of the
  peak is a startup transient in macOS graphics regions (`IOAccelerator`, "Owned physical footprint (graphics)",
  mostly reclaimable). It's gone 2 s later. It also shows up at 640x400 (peak 271 MiB). I didn't measure Linux or
  software GL.

## Exact errors

- `{"value":{"error":"unknown error","message":"Invalid Host header localhost:7101","stacktrace":""}}`
- `{"value":{"error":"unknown error","message":"Invalid Content-Type","stacktrace":""}}`
- New Session twice: `{"value":{"error":"session not created","message":"Session is already started","stacktrace":""}}`
- `GET /session/{id}/cookie/sid` (HttpOnly): `404 no such cookie`
- Add Cookie `.shop.test` / `shop.test` / `other.test` from `www.shop.test`, or anything on about:blank:
  `400 invalid cookie domain`
- `GET /element/{id}/computedlabel`: `500 unsupported operation: Command not implemented: GetComputedLabel(WebElement("…"))`
- Walmart: `ERROR script::dom::html::scripting::htmlscriptelement] Fetching classic script failed Blocked by Content-Security-Policy (UrlWithBlobClaim…`,
  `Unhandled promise rejection: SyntaxError: Failed to parse font face {}`, and repeated `Error at …/_next/static/chunks/framework-31d3b826166ff95f.js:1:115382`
- Walmart with Firefox UA: `ERROR script::dom::workers::workerglobalscope] error loading script chrome://juggler/content (Blocked as mixed content)`
- github with default prefs: `can't access property Symbol.iterator, e.adoptedStyleSheets is undefined`,
  `IntersectionObserver is not defined`

## Verdict per requirement

**Measured** means it's in the tables above. **Opinion** is labelled.

| Requirement | Verdict | Good enough now | Gap, and the concrete work that closes it |
|---|---|---|---|
| Real sites, incl. bot checks | **Much better than the first spike; usable for these sites** | example, github and walmart render and run with site prefs. With a Chrome UA, walmart home 0/2 and search 0/1 challenged, while stock headless Chromium was blocked on search from the same egress. Press and hold via Actions reaches the widget. | **Measured:** Firefox UA challenged 2/2, and PerimeterX probed for Juggler. Missing APIs (WebAuthn, service workers, WebRTC, PaymentRequest). Fingerprint mismatches with a Chrome UA (`webdriver` undefined, empty vendor, no `userAgentData`/`window.chrome`, screen size). **Work (us):** a per-user coherent fingerprint shim (userscript via `--userscripts`, or upstream prefs for vendor, platform and screen), always the Chrome UA, persistent profiles. **Upstream:** set `navigator.webdriver` to `false`, WebAuthn and service workers. **Opinion:** n=5 walmart loads is an anecdote. Servo likely passes partly because PerimeterX has no Servo profile yet, the same caveat as Lightpanda. |
| Agent can read and act on forms | **Yes** | The JS snapshot gives role, name, value and ref on the local login page, github and walmart. Click, send keys, clear and Actions all work. | No native accessibility (Get Computed Role is ARIA-only, Get Computed Label not implemented, AccessKit unchanged in the nightly). **Work (us):** maintain `snapshot.js` (iframes aren't entered yet). **Upstream:** #43734 and #46241. Not blocking. |
| Human takeover | **Feasible over WebDriver, with a relay we build** | 53-55 fps screenshot polling idle, 22 fps while acting. Actions take 22-52 ms per event. Trusted events, holds and clicks replay correctly. | No screencast or push frames, PNG only, and screenshots and input share one serialized session. **Work (us):** a relay that polls Take Screenshot (or diffs frames) and maps browser events to single-action calls. Or embed Servo directly and use `notify_new_frame_ready` (first spike). **Upstream:** a frame-stream or BiDi screencast. **Opinion:** fine for a press-and-hold or a login, rougher than CDP screencast for long sessions. |
| BrowserState round trip (PoC shape) | **Works except HttpOnly export** | `servo_backend.py` fills and applies the PoC's `BrowserState` unchanged. Non-HttpOnly cookies (host and domain), localStorage per origin and tab sessionStorage go both ways, and HttpOnly import works. | **Measured:** Get All Cookies drops HttpOnly, so a Servo-side session login can't be exported. **Work (upstream, small):** switch `handle_get_cookies` / `handle_get_cookie` to `CookieSource::HTTP` (2 call sites). Or carry a patched servoshell, or embed and use `site_data_manager().cookies_for_url(.., HTTP)`, which the first spike showed returns HttpOnly. Also needed: a known origin list, the apex-document trick for domain cookies, and `sameSite` isn't exported. |
| Cost and density | **Worse than Chromium headless shell** | 70-83 ms to a live session (Chromium 61 ms). Load times are on par. | **Measured:** one session per process. 131 MiB settled and 335 MiB peak per extra user, against 20 MiB per extra Chromium context. walmart peaks at 774 MiB against 314. A headless servoshell ignores SIGTERM and SIGINT and outlives Delete Session. **Work (us):** a process-per-user supervisor that SIGKILLs. Measure on Linux (the ~200 MiB peak is macOS graphics). **Upstream:** multiple sessions or user contexts per process. |

## Opinion (not measured)

- **The first spike undersold Servo.** Driven over WebDriver, with prefs on and a Chrome UA, Servo renders real sites
  correctly. An agent can read and fill forms through a JS snapshot with no accessibility support at all. Mike's
  `BrowserState` shape maps onto WebDriver almost entirely. On the one walmart search load each, Servo got through
  where stock headless Chromium didn't. Mike's claim that Servo is "usable" holds for the agent and takeover paths.
- **It isn't a drop-in replacement for Chromium headless shell yet.** Three gaps stay concrete:
  1. HttpOnly cookie export. This is a two-line upstream fix and the most important one, because the hand-off depends
     on it.
  2. Density. A process per user at about 6x Chromium's per-context memory.
  3. No push screencast.

  The bot-check edge is real in this sample, but it most likely comes from Servo being unknown to PerimeterX, not
  from evasion. I wouldn't bank on it lasting. The test was also not like for like: we didn't try a UA-cleaned
  Chromium.
- **Suggested next step if we want Servo as an option:** upstream the `CookieSource::HTTP` fix. Then re-run walmart
  with a Chrome UA plus a small fingerprint userscript from a datacenter egress, and measure per-process memory on
  Linux. Until then, Chromium headless shell stays the default and Servo is a credible second engine.

## Files

- `scripts/wd.py`: a minimal WebDriver client over urllib, the servoshell launcher, prefs and UAs, `footprint`, and
  the RSS sampler
- `scripts/snapshot.js`: the DOM snapshot with roles, names, values and `data-mb-ref` refs
- `scripts/measure.py`: timed site runs and the `--hold` press-and-hold attempt. `scripts/summarize.py` prints the
  per-group medians.
- `scripts/checks.py`: form, github, state, live and density checks. `scripts/live_concurrent.py`: screenshots while
  acting.
- `scripts/servo_backend.py`: `export_state` and `apply_state` for poc's `BrowserState` (imported by path, poc
  unchanged)
- `scripts/apis.py`: the API and identity probe. `scripts/chromium_control.py`: the one Chromium control load.
- `scripts/server.py`: the local server. `scripts/hosts.txt`: the `--host-file` map for the cookie-domain tests.
- `out/results.jsonl`: every run (fingerprint, snapshot head, memory, errors). `out/checks-*.json`, `out/apis.json`,
  `out/servo-browser-state.json` (local test cookies only), `out/*.png` and `out/*-snapshot.txt`.
- `logs/`: servoshell stdout and stderr per run
