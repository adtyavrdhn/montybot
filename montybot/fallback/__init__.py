"""Fallbacks the agent does not use yet, built and tested on their own.

- `montybot.fallback.e2b_desktop`: Chrome on an E2B Desktop sandbox, as a `BrowserBackend` (#22).
- `montybot.fallback.cdp`: the small CDP client it runs inside the desktop VM.

Nothing else under `montybot/` imports this package; `tests/fallback/test_not_imported.py` checks that. See
`README.md` next to this file.
"""
