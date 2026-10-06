"""The browser: the contract every engine implements, the browser service API, and test helpers.

Import from the modules directly; this package re-exports nothing, so each name has one home:

- `montybot.browser.state`: `BrowserState` and `Cookie`, the engine-neutral saved sign-in.
- `montybot.browser.contract`: `BrowserBackend`, the actions, and the errors.
- `montybot.browser.service`: `BrowserService`, the API the agent and the web app call.
- `montybot.browser.host`: `BrowserHost`, the browser service itself, over any backend.
- `montybot.browser.jar`: `SignInJar` and `JarLease`, where saved sign-ins go, with in-memory stand-ins.
- `montybot.browser.fake`: `FakeBrowser`, an in-memory `BrowserBackend`.
- `montybot.browser.conformance`: `BrowserBackendConformance`, the tests every backend passes (needs pytest).
- `montybot.browser.chromium`: `ChromiumBackend`, real Chrome through Playwright (`chromium_linux`: Xvfb and bwrap).

See `README.md` next to this file.
"""
