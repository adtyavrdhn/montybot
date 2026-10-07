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

Jev intent and navigation advice is disabled for now, even when `TYPESAFE_API_KEY` is set. The main agent handles
these decisions directly. Experimental Jev helpers remain available in the source for later evaluation.

## Observability

`LOGFIRE_TOKEN` is optional: without it, no telemetry is sent to Logfire. What is exported is decided per field in
[`montybot/observability.py`](montybot/observability.py):

- **Always:** Pydantic AI's agent, model and tool spans with model, provider and tool names, token and cache usage
  (so Logfire shows cost) and the conversation's shape; database, run,
  browser and Monty timings; the site a browser step opens (host only); outgoing HTTP calls made with httpx, such as
  model provider requests (method, URL and status, never headers or bodies); `run_id` on every span of a run and
  `thread_id`/`user_id` on `run.lifecycle`; exception types; the commit (`service.version`) and `ENVIRONMENT`;
  token and system (CPU, memory) metrics.
- **With `LOGFIRE_INCLUDE_CONTENT` (default on, for the demo):** messages, replies, instructions (with the user's
  memories), the agent's code, page snapshots, and exception messages and tracebacks. Turn it off before real users'
  data flows through.
- **Never:** cookies and browser state, saved sign-ins, passwords typed in live view, session cookies, app secrets,
  hand-off ids and links, push subscription URLs. None reach the agent. HTTP server requests are not traced. Logfire's default scrubbing stays on as a backstop: it replaces values that mention a password,
  cookie, session and so on, including a sign-in page's snapshot.

`tests/e2e/test_traces.py` holds these lines through a whole sign-in hand-off and order, with content on and off.

Each run is a trace of its own, `run.lifecycle` at the top with the agent (`invoke_agent montybot`) inside, unless an
app traced the action that started it (below). Database spans, `run.dispatch` and the live picture's
`browser.peek_screenshot` are recorded only inside a trace: the apps' polling starts none.

The web and Mac apps send their own telemetry too, to the same Logfire project: page loads, Web Vitals, every API
call by route template, the user's actions, the live view, and errors. They post OTLP to `/api/telemetry/v1/...`,
which forwards it with the server's token, for signed-in users only, so neither app holds a token. `GET
/api/telemetry` tells them whether to send anything (only with `LOGFIRE_TOKEN`) and whether to include content. A
traced action sends `traceparent`, and the server's database, run and agent spans for it join the app's trace; there
are still no HTTP server spans, and requests without the header are not traced (the Mac app's polling and the
web app's screenshots send none; the web app's other requests do). Forwarded data
skips the server's scrubbing, so each app keeps to the lines above itself: URLs only as route templates, nothing
from under `/live/`, no passwords, emails or file names, and content only with `LOGFIRE_INCLUDE_CONTENT`.

## Web workspace

The frontend is plain HTML, CSS, and JavaScript in `montybot/static`, with no framework or build step.
Its Pydantic-inspired purple navigation, pink actions, and light conversation surface work on desktop and mobile.
On desktop, chats stay in a persistent sidebar; on a phone, the Chats button opens a keyboard-accessible drawer.

Create an account or sign in, then describe a task in a new chat. Example prompts fill the message box for you to
review before sending. Watch Monty's browser while it works, take over when it asks you to sign in, and answer
questions or approve actions in the chat. Saved sign-ins and schedules are available in the sidebar, alongside
notification opt-in. Motion respects your device's reduced-motion preference.

## Mac app

[`macos/`](macos/README.md) is Monty for Mac: a native SwiftUI client of this server, in Logfire's design. Run
`uv run python macos/scripts/dev_server.py` for a local server it can use, with a scripted model and no Docker.

## Tests

```bash
uv run pytest                                   # unit tests, and end-to-end tests with the fake browser
uv run pytest tests/e2e/test_frontend.py          # responsive UI and local API doubles, no Postgres or model
uv run pytest tests/e2e --browser=chromium      # the same end-to-end tests in real (headless) Chrome
uv run pytest -m u2                             # one user path (u1 ... u6)
MONTYBOT_TEST_MODEL=anthropic:claude-sonnet-4-5 uv run pytest tests/e2e --browser=chromium --live   # real sites, by hand
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
