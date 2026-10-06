# Patches to upstream projects

Local fixes we have not sent upstream yet. Each says what it fixes, why, and how to use it. Nothing here has been
proposed to the upstream project; Mike decides whether and when.

## `servo-webdriver-httponly.patch` (Servo v0.7.0)

**What it fixes.** Servo's WebDriver Get All Cookies and Get Named Cookie leave out HttpOnly cookies. The W3C
WebDriver spec returns every cookie for the current document, HttpOnly included, and Chrome and Firefox do. Without
them, the Servo backend cannot save a sign-in it made itself, or a session cookie a site refreshed while Servo held
it, so `ServoBackend.export()` raises `NotSupported('export')` on a stock build.

**Cause, confirmed by reading the v0.7.0 source** (`git clone --depth 1 --branch v0.7.0
https://github.com/servo/servo`, commit `aac43a3f3`):

- `components/script/event_loop/webdriver_handlers.rs`, `handle_get_cookies` (line 1457) and `handle_get_cookie`
  (line 1484), ask the resource thread for `GetCookiesForUrl(url, sender, NonHTTP)`.
- `NonHTTP` means "as `document.cookie` would see it". `components/net/cookie.rs`,
  `ServoCookie::appropriate_for_url`, drops HttpOnly cookies for that source (line 376), as RFC 6265 says it should
  for page scripts.
- WebDriver is not a page script. `handle_add_cookie` in the same file already picks `HTTP` for an HttpOnly cookie,
  which is why adding one works and reading it back does not.

**The fix.** Ask with `CookieSource::HTTP` at those two call sites. It is two lines; the `HTTP` import is already
there. Deleting cookies needs no change: `DeleteCookie` and `DeleteCookies` ignore the source.

```bash
cd servo            # a checkout of v0.7.0
git apply /path/to/montybot/patches/servo-webdriver-httponly.patch
./mach build --release
```

Then run that build with `ServoOptions(http_only_export=True)`.

**Status.** The patch applies cleanly to v0.7.0 (`git apply --check`). **It has not been built or run**: this Mac has
no Rust toolchain, and a Servo release build needs rustup, `./mach bootstrap` (Homebrew packages) and, per Servo's
docs, a long first build. The backend's export path was tested against stock Servo with states that have no HttpOnly
cookies (`tests/browser/test_servo.py`).

**For an upstream PR.** Servo's WPT expectations list no failing HttpOnly test for these commands
(`tests/wpt/meta/webdriver/tests/classic/get_all_cookies/`), because the WPT tests only use non-HttpOnly cookies. A PR
would want a WPT test that adds an HttpOnly cookie and reads it back.

**A second gap the patch does not fix.** Get All Cookies reports a domain cookie (`Domain=shop.test`) and a host-only
cookie for `shop.test` the same way, as `domain: "shop.test"`, because the `cookie` crate's `domain()` strips the
leading dot and `ServoCookie::host_only` is not passed on. Chrome reports `.shop.test` for the first. Fixing it means
carrying `host_only` from `components/net/cookie_storage.rs` to `components/webdriver_server/lib.rs`
(`cookie_msg_to_cookie`), which is more than a two-line change. The backend works around it without a patch: it
reads the cookie again from a made-up subdomain, where only a domain cookie shows.
