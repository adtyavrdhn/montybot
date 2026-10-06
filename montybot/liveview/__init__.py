"""The live view: the user sees the run's browser, drives it, and gives it back (#14).

- `montybot.liveview.app`: the ASGI app, one WebSocket per hand-off, and the page that embeds it.
- `montybot.liveview.chromium`: `CdpFrameSource`, Chromium's screencast and input over CDP.
- `montybot.liveview.webdriver`: `WebDriverFrameSource`, Servo's screenshots and input over WebDriver.
- `montybot.liveview.polling`: `PollingFrameSource`, for any `BrowserBackend`.
- `montybot.liveview.auth`, `montybot.liveview.handoffs`: what the app needs from the web app and the data model, with
  in-memory stand-ins.
- `montybot.liveview.client`, `montybot.liveview.scripted_user`: a WebSocket client and the scripted user for tests.
- `montybot.liveview.conformance`: the hand-off rules every `BrowserService` passes (needs pytest).

See `README.md` next to this file.
"""
