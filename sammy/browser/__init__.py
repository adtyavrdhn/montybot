"""The browser: the contract every engine implements, the browser service API, and test helpers.

Import from the modules directly; this package re-exports nothing, so each name has one home:

- `sammy.browser.state`: `BrowserState` and `Cookie`, the engine-neutral saved sign-in.
- `sammy.browser.contract`: `BrowserBackend`, the actions, and the errors.
- `sammy.browser.service`: `BrowserService`, the API the agent and the web app call.
- `sammy.browser.live`: `FrameSource`, the live picture and the user's input, for the live view (#14).
- `sammy.browser.host`: `BrowserHost`, the browser service itself, over any backend.
- `sammy.browser.jar`: `SignInJar` and `JarLease`, where saved sign-ins go, with in-memory stand-ins.
- `sammy.browser.snapshot`: `SnapshotWalker`, the snapshot text and refs, from `snapshot.js`, for every engine.
- `sammy.browser.fake`: `FakeBrowser`, an in-memory `BrowserBackend`.
- `sammy.browser.conformance`: `BrowserBackendConformance`, the tests every backend passes (needs pytest).
- `sammy.browser.chromium`: `ChromiumBackend`, real Chrome through Playwright (`chromium_linux`: Xvfb and bwrap).

See `README.md` next to this file.
"""
