# Sammy

Notes and design for an always-on agent built on Monty and Pydantic AI: reachable from chat channels, running in
Monty by default and in a real machine only when a process has to run, with computer use and human takeover.

## 🐿️ Why Sammy is ridiculously, unreasonably awesome

Most agent demos are a `while True:` loop around a chat API. **Sammy is not that.** Sammy is a durable, sandboxed,
browser-driving, app-connecting, cron-scheduling, hand-off-capable agent. It runs on one server, keeps costs down,
and **traces everything it does in Logfire**, from a click in the Mac app to the SQL query that click caused.

You tell it *"every Monday, fill my grocery cart"*, *"tell me when a vet slot opens"* or *"yo what's on my linear"*.
It writes Python, drives a real browser, calls your apps, asks you when it is unsure, hands you the keyboard when a
site wants your password, and pings your phone when it's done.

### 💸 It saves an absurd amount of money

Every layer of Sammy avoids paying for what it doesn't need.

- **Monty first, machines last.** The agent's code runs in **Monty**, a Python interpreter written in Rust that
  starts in microseconds, not in a container or VM that takes seconds to boot and keeps billing while idle
  (`sammy/code.py`). The session is dumped after each call and **no worker is held between calls, during a
  hand-off, or while waiting for approval**. Waiting for a human costs nothing. Real CPython starts only when the
  agent needs pandas or PDFs, in a bubblewrap jail per call with hard caps (`CPYTHON_CPU_SECONDS=60`,
  `CPYTHON_MEMORY_MB=2048`) (`sammy/cpython.py`).
- **Hard limits on runaway code.** Each Monty call gets 60 s of compute, 256 MB of memory and 300 suspensions
  (`ResourceLimits` in `sammy/code.py`). An agent stuck in an infinite loop can't run up your bill.
- **Prompt caching on everything that repeats.** Instructions, tool definitions and the conversation so far (page
  snapshots included) are cached between a run's model calls (`CACHE` in `sammy/agent.py`). The bulk of the
  conversation is billed at cache-read prices instead of full price.
- **Token diets everywhere.**
  - History is trimmed to the last `HISTORY_LIMIT` (40) messages, cut at a user turn so the conversation stays
    whole with no tool call cut from its result (`recent()` in `sammy/workflows.py`).
  - Pages reach the model as a compact text outline (headings, text, numbered controls) of at most 12,000
    characters, not raw HTML or a screenshot for every step (`sammy/browser/snapshot.py`).
  - Images are shrunk to 1568 px before the model sees them. Earlier turns' files come back only within a 16 MB
    budget (`sammy/attachments.py`).
  - Stored history keeps a short note per file, not the bytes.
- **Crashes don't cost twice.** Every run is a **DBOS workflow**. Model requests, browser calls, memory writes and
  schedule changes are steps, and each step's result is recorded. If the server dies halfway through a run, DBOS
  starts it again and **every finished step returns its recorded result instead of running again**, so model calls
  that already finished are not paid for again (`sammy/workflows.py`).
- **Browsers that pay rent only when used.** A user's runs share **one** Chromium, a tab each. An idle browser is
  saved and closed after a day (`BROWSER_IDLE_TIMEOUT_SECONDS`). `BROWSER_MAX_OPEN` caps live browsers (8 on the
  server, about 430 MB each), evicting the least recently used. The live view streams Chromium's screencast, so **a
  still page sends nothing** (`sammy/browser/host.py`, `sammy/liveview/`).
- **One box, no fleet.** Everything is Caddy, the app, Postgres, nightly backups and an egress proxy on **one Linux
  server** with Docker Compose (`deploy/`). DBOS runs on the same Postgres, so there is no Redis, no queue service, no
  Kubernetes and no separate workflow cluster. The web app is plain HTML, CSS and JS with **no build step**.
- **Watches, not polling humans.** A schedule can be a *watch* ("tell me when a slot opens"): it calls
  `notify_user` once, by push and email, then **pauses itself** so it stops spending (`sammy/schedule_tools.py`).
- **Telemetry that doesn't bloat your bill.** The apps' polling never starts a trace (`only_in_trace=True`). Client
  telemetry is capped at 120 exports and 10 MB per minute (`TELEMETRY_PER_MINUTE`). And because token usage and cost
  are on every model span, **you can see exactly where the money goes** (below).

### 🪵 Logfire everywhere, and we mean *everywhere*

Sammy isn't just "instrumented with Logfire". It is **built around** Logfire. Set one `LOGFIRE_TOKEN` and every
layer reports in, in **one connected trace**.

- **One call wires it all** (`sammy/observability.py`): `logfire.configure` with service name, commit
  (`service.version`) and environment, then `instrument_pydantic_ai`, `instrument_httpx` and
  `instrument_system_metrics`. Agent runs, every model request, every tool call, **token usage, cache hits and
  cost**, every outgoing HTTP call to a model provider, CPU and memory.
- **44 hand-named spans across the whole system:**
  - runs: `run.lifecycle`, `run.start`, `run.agent`, `run.dispatch`, `run.finish`, `run.fail`, `run.close`
  - Monty: `monty.run`, `monty.session`, `monty.snippet`, `monty.dump`, `monty.load`
  - the agent driving the browser: `code.browser.goto`, `code.browser.click`, `code.browser.type`,
    `code.browser.read`
  - the browser itself: `browser.snapshot`, `browser.act`, `browser.handoff.start`, `browser.handoff.end`,
    `browser.state.save`, `browser.reap_idle`, `browser.live_view`
  - the database: `db.query`, `db.pool.acquire`, `db.migrate`
  - ...and more. Every layer, every hop.
- **`run_id` on every span of a run** through OpenTelemetry baggage. Model calls, browser calls and Monty snippets
  all carry it, and `run.lifecycle` adds `thread_id`, `user_id` and `trigger` (message or schedule). Each browser
  step records the **site it opened** (host only).
- **One trace from the click to the database.** The web app (Logfire's browser SDK, `sammy/static/telemetry.js`)
  and the native Mac app (OpenTelemetry Swift, `macos/Sources/SammyKit/Telemetry.swift`) trace page loads, **Web
  Vitals**, long animation frames, clicks, submits, every API call by route template, the live view and errors.
  Every traced action sends a W3C `traceparent`, and `ClientTraceContext` puts the server's spans **inside the
  client's trace**. One trace covers the click, the API call, the DBOS workflow, the agent, the model, Monty and
  Chromium.
- **No tokens in clients, ever.** Both apps post OTLP to `/api/telemetry/v1/...`, and the server forwards it with
  *its* token, for signed-in users only. `GET /api/telemetry` tells the apps whether to send anything and whether
  content is allowed.
- **Error rates that mean bugs.** `kind_of()` sorts every exception into `error`, `expected` or `cancelled`. A page
  that didn't load, or a mistake in the agent's own code, is a **warning**. A user pressing stop is just cancelled.
  Only real failures turn spans red.
- **Privacy by construction.** Every field is classed as *always*, *only with `LOGFIRE_INCLUDE_CONTENT`*, or
  *never* (see [Observability](#observability)). Clients rewrite URLs to route templates such as
  `/api/threads/{thread_id}`, hand-off links become `/live/*`, and long ids become `{id}`. MCP server URLs are never
  traced. Logfire's scrubbing stays on as a backstop. Cookies, passwords and sign-ins never leave.

### 🔌 It plugs into everything you use

- **About 120 apps in one click through Composio:** Linear, GitHub, Gmail, Notion, Slack and more. Sammy creates its
  own auth config per app on first use, and each user's accounts are namespaced so a Composio project can be shared
  (`sammy/integrations/composio.py`).
- **Bring your own MCP servers:** streamable HTTP with a header, or a full OAuth sign-in with discovery, dynamic
  client registration, PKCE and refresh (`sammy/integrations/mcp.py`, `oauth.py`). URLs, headers and tokens are
  sealed with the user's own data key, and every connection is **checked to reach public addresses only**
  (`sammy/integrations/egress.py`).
- **Three tools that never change** (`sammy/integration_tools.py`), however many apps are connected: list, call and
  connect. The model's tool list stays small, and the prompt cache stays warm. Anything that changes something
  **asks you first**.
- **Connect in the middle of a task.** Say "what's on my linear" without Linear connected, and the chat shows a
  connect card. Sign in, and **the same run carries on**.
- **A real browser with your sign-ins.** Chromium in a bubblewrap jail, driven over Sammy's own CDP pipe, behind a
  public-only SOCKS egress proxy. Saved sign-ins use **envelope encryption** (AES-256-GCM, a per-user data key
  wrapped by the deployment key, with associated data tied to the user and version) (`sammy/signins.py`,
  `sammy/crypto.py`).
- **Human takeover.** When a site wants a password or a CAPTCHA, Sammy hands you the live browser, with an editable
  address bar and tabs, and picks up when you hand it back.
- **Files both ways.** Drop up to 10 files per message (20 MB each). Sammy reads images, PDFs and text directly,
  opens spreadsheets with real CPython, and hands files back with `share_file`.
- **Memory, schedules and notifications.** Per-user memories, cron schedules in your own time zone, watches, and web
  push plus email when Sammy needs you or finishes.
- **Everywhere you are.** A framework-free web app that works on phones, and a native SwiftUI **Mac app** with a
  menu bar, a command palette and an animated 3D squirrel. 🐿️

In short: Sammy does the work, spends as little as possible doing it, and Logfire shows every step, with the
model's cost on every request. ✨

## Design

[`DESIGN.md`](DESIGN.md) is the current design for Sammy: scheduled browser runs in Monty and a sandboxed
Chromium, DBOS for schedules, and hand-off to the user when the agent gets stuck.

## Run it

```bash
docker compose up -d          # Postgres
cp .env.example .env          # then set SESSION_SECRET, MONTY_EXECUTION_KEY and your model's API key
uv run sammy serve            # http://127.0.0.1:8000
```

The app is Starlette plus DBOS in one process (`sammy/app.py`, `sammy/workflows.py`). A run is a DBOS workflow:
model requests and browser calls are steps, and questions, approvals and hand-offs wait in `DBOS.recv`
(`sammy/approvals.py`). The agent's code runs in Monty through `run_code` (`sammy/code.py`), with the browser
as host functions; with `MONTY_URL` and `MONTY_EXECUTION_KEY` set it runs on the hosted Monty sandboxes (see
[`deploy/README.md`](deploy/README.md#hosted-monty-sandboxes)). Our own monty-server is still there, behind
`docker compose --profile full-monty`, but no longer used by default. Its file calls (`pathlib`, `open`) reach the user's
own directory under `WORKSPACES_DIR` at `/work`, where browser downloads land too (`sammy/workspaces.py`). Heavy
Python (pandas, PDFs) runs through `run_python` in real CPython, in a bubblewrap jail per call on the same files
(`sammy/cpython.py`, Linux only). The browser contract and service are in [`sammy/browser/`](sammy/browser/README.md).

Files live in the chats (`sammy/attachments.py`). The user drops, pastes or picks files in the composer (up to 10
per message, 20 MB each). Each file uploads at once and goes with the next message. The model sees images (shrunk to
1568 px), PDFs and text files directly. Every file is also saved in `/work/uploads` for the code tools, which is how
Sammy opens spreadsheets, Word documents and the like. The stored history keeps only a note per file. The bytes stay
in Postgres, and later turns get the files back within a size budget. Sammy gives files back with the `share_file`
tool, and they appear on its reply.

Jev intent and navigation advice is disabled for now, even when `TYPESAFE_API_KEY` is set. The main agent handles
these decisions directly. Experimental Jev helpers remain available in the source for later evaluation.

## Voice

On a phone, talking is faster than typing (`sammy/voice.py`, `sammy/static/voice.js`). The microphone in the
composer dictates a message into the message box, to read over and send. The web app uses the browser's own speech
recognition where there is one (Safari on iPhone and Mac, Chrome); the Mac app uses Apple's. Elsewhere it records,
and the server turns the recording into text. Each Sammy reply has a speaker button that reads it aloud, and "Read
replies aloud" (in the menu, kept per browser) reads each new reply once its task is done, and each question Sammy
asks while you watch.

Set `VOICE_PROVIDER` (`openai` or `elevenlabs`) and `VOICE_API_KEY` for the server's part: transcribing recordings
(`POST /api/voice/transcriptions`) and reading replies in the provider's voice (`POST /api/voice/speech`; `VOICE_NAME`
picks the voice). Unset, dictation needs the browser's own speech recognition, and replies are read in the device's
voice. The key stays on the server. A recording is held in memory for the one request and is never stored.

## Integrations

Users connect the services they use, and Sammy works in them through three agent tools that never change
(`sammy/integration_tools.py`): `list_integration_tools`, `call_integration_tool` (a tool that changes something
asks the user first, as `commit` does) and `connect_integration`. When a request needs a service that is not
connected ("yo what's on my linear"), the agent calls `connect_integration`, and the chat shows a card to connect it
(an ask of kind `connect`); the run carries on once the sign-in finishes. Each user's connections are listed, added
and removed on the Integrations page of the web and Mac apps.

- **Apps through Composio** (`sammy/integrations/composio.py`): set `COMPOSIO_API_KEY` and every app with
  Composio-managed OAuth (about 120: Linear, GitHub, Gmail, Notion, Slack...) connects in one click. Sammy makes its
  own auth config per app (`sammy-<app>`) on first use. Each user is `COMPOSIO_USER_PREFIX` + their id in
  Composio; Sammy lists, uses and removes only accounts under that id, and runs each tool on the user's own account,
  so a Composio project can be shared with other apps (give each its own prefix).
- **The user's own MCP servers** (`sammy/integrations/mcp.py`, `oauth.py`): a streamable HTTP URL with a header,
  or an OAuth sign-in (discovery, dynamic client registration, PKCE, refresh). The URL, headers and tokens are sealed
  with the user's data key. Requests go to public addresses only, checked as each connection opens
  (`sammy/integrations/egress.py`), and are never traced, as a server's URL can hold a key.

## Observability

`LOGFIRE_TOKEN` is optional: without it, no telemetry is sent to Logfire. What is exported is decided per field in
[`sammy/observability.py`](sammy/observability.py):

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
  hand-off ids and links, push subscription URLs, voice recordings and the words in them or read aloud. None reach
  the agent. HTTP server requests are not traced. Logfire's default scrubbing stays on as a backstop: it replaces values that mention a password,
  cookie, session and so on, including a sign-in page's snapshot.

`tests/e2e/test_traces.py` holds these lines through a whole sign-in hand-off and order, with content on and off.

Each run is a trace of its own, `run.lifecycle` at the top with the agent (`invoke_agent sammy`) inside, unless an
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

The frontend is plain HTML, CSS, and JavaScript in `sammy/static`, with no framework or build step.
Its Pydantic-inspired purple navigation, pink actions, and light conversation surface work on desktop and mobile.
On desktop, chats stay in a persistent sidebar; on a phone, the Chats button opens a keyboard-accessible drawer.

Create an account or sign in, then describe a task in a new chat. Example prompts fill the message box for you to
review before sending. Watch Sammy's browser while it works, take over when it asks you to sign in, and answer
questions or approve actions in the chat. Saved sign-ins and schedules are available in the sidebar, alongside
notification opt-in. Motion respects your device's reduced-motion preference.

## Mac app

[`macos/`](macos/README.md) is Sammy for Mac: a native SwiftUI client of this server, in Logfire's design. Run
`uv run python macos/scripts/dev_server.py` for a local server it can use, with a scripted model and no Docker.

## Tests

```bash
uv run pytest                                   # unit tests, and end-to-end tests with the fake browser
uv run pytest tests/e2e/test_frontend.py          # responsive UI and local API doubles, no Postgres or model
uv run pytest tests/e2e --browser=chromium      # the same end-to-end tests in real (headless) Chrome
uv run pytest -m u2                             # one user path (u1 ... u6)
SAMMY_TEST_MODEL=anthropic:claude-sonnet-4-5 uv run pytest tests/e2e --browser=chromium --live   # real sites, by hand
tests/linux/run.sh tests/test_cpython.py tests/e2e/test_files.py   # the tests that need Linux and bwrap, in Docker
```

End-to-end tests run the real app in its own process against Postgres (`SAMMY_TEST_POSTGRES`, or a container they
start with Docker), the fixture sites in `tests/sites` (one per user path), a scripted model (`tests/e2e/scripts.py`)
and a scripted human who drives hand-offs through the live-view API. With `SAMMY_TEST_MODEL` the same tests run
against a real model; `--live` adds real sites (`tests/e2e/test_live.py`). The CPython tier's tests need Linux with
bwrap and are skipped elsewhere; `tests/linux/run.sh` runs them in an Ubuntu container on any Docker host (colima on a
Mac), reaching `SAMMY_TEST_POSTGRES` and `SAMMY_TEST_MONTY_URL` on the Docker host's network.

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
