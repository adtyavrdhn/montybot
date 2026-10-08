# Servo embedding spike

A throwaway check of whether the `servo` crate (0.7.0 from crates.io, MPL-2.0) could stand in for headless Chromium in
Sammy. It's one Rust binary that runs Servo offscreen with `SoftwareRenderingContext` and does the following:

- loads a URL and waits for `LoadStatus::Complete`
- saves a PNG screenshot
- dumps the AccessKit accessibility tree as role and name
- imports cookies before the load and exports them afterwards
- on the local login page, clicks an input, types into it, then presses the button through an AccessKit `Click` action

Tested on macOS arm64 with rustc 1.95.0 on 2026-10-06.

## Build

```sh
cd spikes/servo/servo-spike
cargo build --release      # cold: ~150 s wall on an M-series Mac, 3.4 GB target/
```

No Homebrew packages were needed. `mozjs_sys` downloads a prebuilt SpiderMonkey
(`libmozjs-aarch64-apple-darwin.tar.gz` from github.com/servo/mozjs/releases). The crate is pinned to `=0.7.0` with the
`webcrypto` feature on. Without that feature, `window.crypto` doesn't exist and walmart.com fails.

## Run

```sh
cd spikes/servo
python3 -m http.server 8765 --bind 127.0.0.1 --directory site &   # serves site/login.html

BIN=servo-spike/target/release/servo-spike
$BIN http://127.0.0.1:8765/login.html --out out/login.png --form-test
$BIN https://example.com/ --out out/example.png
$BIN https://github.com/login --experimental --out out/github.png
$BIN https://www.walmart.com/ --experimental --timeout 60 --out out/walmart.png
$BIN https://example.com/ --two-instances          # tries to build a second Servo in the same process

# timing + /usr/bin/time -l wrapper (writes logs/run-<name>.log and out/<name>.png)
python3 scripts/measure.py github https://github.com/login --experimental
```

Flags:

| Flag | What it does |
|---|---|
| `--out` | Path for the PNG screenshot |
| `--form-test` | Runs the click, typing and AccessKit-click test against `site/login.html` |
| `--experimental` | Turns on prefs that are off by default but that real sites need: IntersectionObserver, ResizeObserver, `crypto.subtle`, FontFace, IndexedDB, Web Animations and adoptedStyleSheets |
| `--two-instances` | Tries to build a second Servo in the same process |
| `--width`, `--height` | Viewport size, 1280x800 by default |
| `--timeout` | Seconds to wait for the load, 45 by default |
| `--a11y-lines` | Maximum number of lines in the accessibility dump |

The first run after relinking spends about 1.7 s before `main` starts (macOS checks the new binary), so run it once
before taking timings.

## Where things are in `servo-spike/src/main.rs`

| Step | API |
|---|---|
| Offscreen GL | `SoftwareRenderingContext::new`, `make_current` |
| Engine | `ServoBuilder::default().preferences(..).event_loop_waker(..).build()`, `Servo::spin_event_loop` |
| WebView | `WebViewBuilder::new(&servo, ctx).delegate(..).url(..).build()` |
| Load and paint signals | `WebViewDelegate::notify_load_status_changed` and `notify_new_frame_ready` (which calls `webview.paint()`) |
| Screenshot | `WebView::take_screenshot(None, cb)` returns an `RgbaImage`, saved with `image` |
| Accessibility | `Preferences::accessibility_enabled = true`, `WebView::set_accessibility_active(true)`, `WebViewDelegate::notify_accessibility_tree_update` (merged into a node map in `A11yTree`) |
| AccessKit action | `Servo::forward_accessibility_action(ActionRequest { action: Action::Click, .. })` |
| Cookies | `servo.site_data_manager().set_cookie_for_url(url, cookie::Cookie, None)` and `cookies_for_url(url, CookieSource::HTTP or NonHTTP)` |
| Input | `WebView::notify_input_event(InputEvent::MouseMove, MouseButton or Keyboard)` with `WebViewPoint::Page` coordinates |
| JavaScript | `WebView::evaluate_javascript(script, cb)` |
| Console | `WebViewDelegate::show_console_message` (prints errors and warnings) |

## Output

- `logs/build1.log` is the cold build log. It ends in a compile error in the spike's own `main.rs`, because the
  direct dependencies weren't declared yet; all of the Servo dependencies had compiled by then.
- `logs/run-*.log` holds the output of each run, including the `/usr/bin/time -l` numbers.
- `out/*.png` holds the screenshots.
