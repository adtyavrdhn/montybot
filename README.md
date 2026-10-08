# montybot

Notes and design for an always-on agent built on Monty and Pydantic AI: reachable from chat channels, running in
Monty by default and in a real machine only when a process has to run, with computer use and human takeover.

## Design

[`DESIGN.md`](DESIGN.md) is the current design for monty-bot: scheduled browser runs in Monty and a sandboxed
Chromium, DBOS for schedules, and hand-off to the user when the agent gets stuck.

## Run it

```bash
docker compose up -d          # Postgres
cp .env.example .env          # then set SESSION_SECRET, MONTY_EXECUTION_KEY and your model's API key
uv run montybot serve         # http://127.0.0.1:8000
```

The app is Starlette plus DBOS in one process (`montybot/app.py`, `montybot/workflows.py`). A run is a DBOS workflow:
model requests and browser calls are steps, and questions, approvals and hand-offs wait in `DBOS.recv`
(`montybot/approvals.py`). The agent's code runs in Monty through `run_code` (`montybot/code.py`), with the browser
as host functions; with `MONTY_URL` and `MONTY_EXECUTION_KEY` set it runs on the hosted Monty sandboxes (see
[`deploy/README.md`](deploy/README.md#hosted-monty-sandboxes)). Our own monty-server is still there, behind
`docker compose --profile full-monty`, but no longer used by default. Its file calls (`pathlib`, `open`) reach the user's
own directory under `WORKSPACES_DIR` at `/work`, where browser downloads land too (`montybot/workspaces.py`). Heavy
Python (pandas, PDFs) runs through `run_python` in real CPython, in a bubblewrap jail per call on the same files
(`montybot/cpython.py`, Linux only). The browser contract and service are in [`montybot/browser/`](montybot/browser/README.md).

Files live in the chats (`montybot/attachments.py`). The user drops, pastes or picks files in the composer (up to 10
per message, 20 MB each). Each file uploads at once and goes with the next message. The model sees images (shrunk to
1568 px), PDFs and text files directly. Every file is also saved in `/work/uploads` for the code tools, which is how
Monty opens spreadsheets, Word documents and the like. The stored history keeps only a note per file. The bytes stay
in Postgres, and later turns get the files back within a size budget. Monty gives files back with the `share_file`
tool, and they appear on its reply.

Jev intent and navigation advice is disabled for now, even when `TYPESAFE_API_KEY` is set. The main agent handles
these decisions directly. Experimental Jev helpers remain available in the source for later evaluation.

## Integrations

Users connect the services they use, and Monty works in them through three agent tools that never change
(`montybot/integration_tools.py`): `list_integration_tools`, `call_integration_tool` (a tool that changes something
asks the user first, as `commit` does) and `connect_integration`. When a request needs a service that is not
connected ("yo what's on my linear"), the agent calls `connect_integration`, and the chat shows a card to connect it
(an ask of kind `connect`); the run carries on once the sign-in finishes. Each user's connections are listed, added
and removed on the Integrations page of the web and Mac apps.

- **Apps through Composio** (`montybot/integrations/composio.py`): set `COMPOSIO_API_KEY` and every app with
  Composio-managed OAuth (about 120: Linear, GitHub, Gmail, Notion, Slack...) connects in one click. Monty makes its
  own auth config per app (`montybot-<app>`) on first use. Each user is `COMPOSIO_USER_PREFIX` + their id in
  Composio; Monty lists, uses and removes only accounts under that id, and runs each tool on the user's own account,
  so a Composio project can be shared with other apps (give each its own prefix).
- **The user's own MCP servers** (`montybot/integrations/mcp.py`, `oauth.py`): a streamable HTTP URL with a header,
  or an OAuth sign-in (discovery, dynamic client registration, PKCE, refresh). The URL, headers and tokens are sealed
  with the user's data key. Requests go to public addresses only, checked as each connection opens
  (`montybot/integrations/egress.py`), and are never traced, as a server's URL can hold a key.

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
- **Never:** cookies and browser state, saved sign-ins, integration credentials and MCP server URLs, passwords typed
  in live view, session cookies, app secrets,
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
are still no HTTP server spans, and requests without the header are not traced (neither app's polling sends
one). Forwarded data
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
