# Takeover spike

Hands a headless Chromium to a human through a streamed web page, then saves what they signed in to as one
portable file.

```
uv run takeover.py https://github.com/login --identity me    # open the printed link, sign in, "Return control"
uv run --with playwright --with aiohttp python selftest.py   # end-to-end check against a local login site
```

Identity files (`identities/*.json`) hold live session cookies. They are gitignored; never commit them.

How it works: `takeover.py` starts headless Chromium with `storage_state` from `identities/<name>.json` if present,
streams the tab with CDP `Page.startScreencast` over a WebSocket to a canvas, replays clicks and keys with
Playwright's mouse and keyboard, and on "Return control" writes `context.storage_state(indexed_db=True)`. The link is
single-use. The self-test proves an HttpOnly cookie and localStorage written during the takeover come back in a
brand-new browser.
