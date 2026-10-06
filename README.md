# montybot

Notes and design for an always-on agent built on Monty and Pydantic AI: reachable from chat channels, running in
Monty by default and in a real machine only when a process has to run, with computer use and human takeover.

## Design

[`DESIGN.md`](DESIGN.md) is the current design for monty-bot: scheduled browser runs in Monty and a sandboxed
Chromium, DBOS for schedules, and hand-off to the user when the agent gets stuck.

## Run it

```bash
docker compose up -d          # Postgres, monty-server and monty-worker
cp .env.example .env          # then set SESSION_SECRET and your model's API key
uv run montybot serve         # http://127.0.0.1:8000
```

The app is Starlette plus DBOS in one process (`montybot/app.py`, `montybot/workflows.py`). A run is a DBOS workflow:
model requests and browser calls are steps, and questions, approvals and hand-offs wait in `DBOS.recv`
(`montybot/approvals.py`). The agent's code runs in Monty through `run_code` (`montybot/code.py`), with the browser
as host functions; with `MONTY_URL` set it runs on Full Monty. Its file calls (`pathlib`, `open`) reach the user's
own directory under `WORKSPACES_DIR` at `/work`, where browser downloads land too (`montybot/workspaces.py`). Heavy
Python (pandas, PDFs) runs through `run_python` in real CPython, in a bubblewrap jail per call on the same files
(`montybot/cpython.py`, Linux only). The browser contract and service are in [`montybot/browser/`](montybot/browser/README.md).

## Observability

`LOGFIRE_TOKEN` is optional: without it, no telemetry is sent to Logfire. With it, the app exports fixed-label
HTTP, database pool/query, run, browser and Monty durations, HTTP response status, and numeric model token usage
(including prompt-cache reads/writes). Cache usage is part of the model request span: providers do not expose a
separate cache duration. Trace/span IDs correlate operations; user content is not needed for tracing.

Pydantic AI keeps `include_content=False` and passes through an allowlist adapter before export. Messages, tool
arguments/results, code, page contents, typed input, cookies/storage state, credentials, hand-off IDs/links, full
URLs and exception text are excluded. HTTP/SQL auto-instrumentation and model metrics are deliberately disabled
because they can expose URL, query or provider metadata. Keep operation names literal and do not add argument
capture when extending instrumentation. `tests/e2e/test_traces.py` checks the exported privacy boundary.

## Tests

```bash
uv run pytest                                   # unit tests, and end-to-end tests with the fake browser
uv run pytest tests/e2e --browser=chromium      # the same end-to-end tests in real (headless) Chrome
uv run pytest -m u2                             # one user path (u1 ... u6)
MONTYBOT_TEST_MODEL=anthropic:claude-sonnet-4-5 uv run pytest tests/e2e --browser=chromium --live   # nightly
tests/linux/run.sh tests/test_cpython.py tests/e2e/test_files.py   # the tests that need Linux and bwrap, in Docker
```

End-to-end tests run the real app in its own process against Postgres (`MONTYBOT_TEST_POSTGRES`, or a container they
start with Docker), the fixture sites in `tests/sites` (one per user path), a scripted model (`tests/e2e/scripts.py`)
and a scripted human who drives hand-offs through the live-view API. With `MONTYBOT_TEST_MODEL` the same tests run
against a real model; `--live` adds real sites (`tests/e2e/test_live.py`). The CPython tier's tests need Linux with
bwrap and are skipped elsewhere; `tests/linux/run.sh` runs them in an Ubuntu container on any Docker host (colima on a
Mac), reaching `MONTYBOT_TEST_POSTGRES` and `MONTYBOT_TEST_MONTY_URL` on the Docker host's network.

## Notes

Dated files in [`notes/`](notes/), newest last. Later notes win over earlier ones.

- [2026-10-06 clai2 vs Muse, Dots and Grok Bot](<notes/2026-10-06 clai2 vs Muse, Dots and Grok Bot.md>): what Meta
  Muse, OpenAI dots and xAI Grok Bot do, where clai2 stands, and designs for channels, Monty-first execution,
  E2B computer use, and sessions and takeover.
- [2026-10-06 browser, isolation and state flow](<notes/2026-10-06 browser, isolation and state flow.md>): Ladybird,
  Lightpanda and Servo as Monty's browser, two browser tiers, per-user browser state, and isolation between users.
- [2026-10-06 monty-bot plan and browser design](<notes/2026-10-06 monty-bot plan and browser design.md>): the goals
  agreed so far (a hosted consumer bot built on Viktor), how the browser and user takeover work on one server, bot
  checks, speed, and milestones. Draft for discussion with Mike.
