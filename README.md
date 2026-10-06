# montybot

Notes and design for an always-on agent built on Monty and Pydantic AI: reachable from chat channels, running in
Monty by default and in a real machine only when a process has to run, with computer use and human takeover.

## Design

[`DESIGN.md`](DESIGN.md) is the current design for monty-bot: scheduled browser runs in Monty and a sandboxed
Chromium, DBOS for schedules, and hand-off to the user when the agent gets stuck.

## Run it

```bash
docker compose up -d          # Postgres
cp .env.example .env          # then set SESSION_SECRET and your model's API key
uv run montybot serve         # http://127.0.0.1:8000
```

The app is Starlette plus DBOS in one process (`montybot/app.py`, `montybot/workflows.py`). A run is a DBOS workflow:
model requests and browser calls are steps, and questions, approvals and hand-offs wait in `DBOS.recv`
(`montybot/approvals.py`). The browser contract and service are in [`montybot/browser/`](montybot/browser/README.md).

## Tests

```bash
uv run pytest       # unit tests, and end-to-end tests with the fake browser
```

End-to-end tests run the real app in its own process against Postgres (`MONTYBOT_TEST_POSTGRES`, or a container they
start with Docker), the fixture sites in `tests/sites`, a scripted model (`tests/e2e/scripts.py`) and a scripted
human who drives hand-offs through the live-view API.

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
