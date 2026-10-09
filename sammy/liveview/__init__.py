"""The live view: the user sees the run's browser, drives it, and gives it back (#14).

- `sammy.liveview.app`: the ASGI app, one WebSocket per hand-off, and the page that embeds it.
- `sammy.liveview.chromium`: `CdpFrameSource`, Chromium's screencast and input over CDP.
- `sammy.liveview.webdriver`: `WebDriverFrameSource`, Servo's screenshots and input over WebDriver.
- `sammy.liveview.polling`: `PollingFrameSource`, for any `BrowserBackend`.
- `sammy.liveview.auth`, `sammy.liveview.handoffs`: what the app needs from the web app and the data model, with
  in-memory stand-ins.
- `sammy.liveview.client`, `sammy.liveview.scripted_user`: a WebSocket client and the scripted user for tests.
- `sammy.liveview.conformance`: the hand-off rules every `BrowserService` passes (needs pytest).

See `README.md` next to this file.
"""
