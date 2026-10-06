"""The browser: the contract every engine implements, the browser service API, and test helpers.

Import from the modules directly; this package re-exports nothing, so each name has one home:

- `montybot.browser.state`: `BrowserState` and `Cookie`, the engine-neutral saved sign-in.
- `montybot.browser.contract`: `BrowserBackend`, the actions, and the errors.
- `montybot.browser.service`: `BrowserService`, the API the agent and the web app call.
- `montybot.browser.live`: `FrameSource`, the live picture and the user's input, for the live view (#14).
- `montybot.browser.fake`: `FakeBrowser`, an in-memory `BrowserBackend`.
- `montybot.browser.conformance`: `BrowserBackendConformance`, the tests every backend passes (needs pytest).

See `README.md` next to this file.
"""
