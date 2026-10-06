# monty-bot build plan

**Status:** written 2026-10-06 by Claude for Aditya. A starting plan to flesh out as we go. It follows
[`2026-10-06 monty-bot plan and browser design.md`](<2026-10-06 monty-bot plan and browser design.md>), Mike's
[`DESIGN.md`](../DESIGN.md) and his hand-off proof of concept in [`poc/`](../poc/). Viktor facts are from its `main`
at `c1896df`; Pydantic AI facts from `main` at `803d3eb46` or later.

**Superseded in part:** the GitHub issues (#1 to #22) are now the plan. Since this note: Chromium and Servo both get a
fair shot behind one contract (#18), deploy is Docker Compose on one server (#7), and the web app comes before Slack.

## Rulings

- **Independent codebase.** monty-bot lives in this repo. We copy Viktor's code, structure and infra decisions and
  change them freely. No shared library for now.
- **DBOS, not pgtask.** Viktor uses pgtask; monty-bot uses DBOS through Pydantic AI's `DBOSDurability` capability.
- **Chrome, one per user run, owned by a browser service.** From the design note.
- **Everything else is open to questions as we go.**

## What Mike's proof of concept adds

`poc/` moves a browser session from a remote headless Chrome to a sandboxed Chrome window on the user's own machine
and back. The user signs in locally and clicks "Return control to agent".

- `poc/montybot_poc/state.py` `BrowserState` is the format to adopt: URL, cookies (HttpOnly included),
  localStorage, and the handed-over tab's sessionStorage. It is engine-neutral.
- `poc/montybot_poc/browser.py` seeds a context from a `BrowserState` (sessionStorage through an init script) and
  exports one back.
- It closes the remote context before sending the state and opens a new one from what comes back, so there is only
  ever one writer.
- Open, per its README: whether real sites accept a session that moves between a home address and a data centre,
  and that the local window reports `navigator.webdriver = true`.

So there are two ways to hand the browser to a person, sharing one state format:

| | Live view (design note) | Local window (Mike's `poc/`) |
|---|---|---|
| Where the user signs in | Our Chrome, through a web page | A Chrome window on their own machine |
| Works on a phone | Yes | No, needs a desktop app |
| Install needed | None | A small local app |
| Address the site sees at sign-in | Our proxy | The user's home address |
| Page state kept | Yes, same Chrome | Only what `BrowserState` carries |
| Our cost while waiting | A warm Chrome for up to N minutes | Nothing |

Both are built on the same pieces: `BrowserState`, the browser service, and a "hand-off" approval in the agent loop.
Which one comes first is an open question below.

## Repo layout

Copied from Viktor's layout, with `viktor/` renamed and the browser added:

```
montybot/
  pyproject.toml, uv.lock, Makefile, Dockerfile, .env.example
  montybot/
    app.py, settings.py, resources.py, observability.py     from Viktor
    db.py, models.py, store.py, migrations/                 rewritten: everything keyed by user
    agent.py, feature.py, deps.py                           from Viktor; new instructions and feature list
    workflows.py                                            new: DBOS workflows (replaces handlers.py + tasks.py)
    approvals.py                                            from Viktor; DBOS.recv instead of wait_for_signal
    scheduler.py, scheduler_tools.py                        from Viktor; DBOS.create_schedule
    memory.py, consolidation.py, skills.py, history.py      from Viktor
    progress.py, metering.py, credits.py, model_catalogue.py from Viktor
    surface.py, slack_client.py, slack_events.py, slack_actions.py   from Viktor
    api.py, auth.py                                         from Viktor (web app backend, later)
    browser/
      state.py                                              from poc/ (BrowserState)
      service.py                                            new: the browser service process
      tools.py                                              new: Monty-side browser functions (thin client)
      handoff.py                                            new: live view and/or local window
      chrome-in-bwrap                                       from DESIGN.md
  tests/, evals/                                            from Viktor's patterns
  poc/, spikes/, notes/, DESIGN.md                          stay as they are
```

Each copied file starts with a comment naming the Viktor commit it came from, so fixes can be brought across by hand.

## pgtask to DBOS

| Viktor (pgtask) | monty-bot (DBOS) |
|---|---|
| `PGTaskDurability` capability on the agent | `DBOSDurability` capability (`pydantic_ai.durable_exec.dbos`) |
| A run is a pgtask task, `handlers.execute` | A run is a `@DBOS.workflow`, `workflows.run` |
| `durable_step(name, operation)` (`durable.py`) | `@DBOS.step` functions, or `DBOS.run_step` |
| `task.wait_for_signal(...)` in `approvals.py` | `DBOS.recv(topic, timeout_seconds=...)` inside the workflow |
| `emit_signal` from the web process | `DBOS.send(workflow_id, message, topic)` |
| pgtask schedules + `scheduled_tasks` table | `DBOS.create_schedule` / `pause_schedule` / `delete_schedule`, one per user schedule |
| pgtask workers and queues (`worker.py`, `queue.py`) | DBOS queues in the same process; N copies of the app |
| Every tool call checkpointed | Model requests and MCP calls are steps; **our own tools must be steps themselves** |

Two things to check in step 1, before copying more:

1. **Viktor's per-run features.** `Feature.per_run` builds a `DynamicCapability` (`viktor/feature.py:133`).
   `DBOSDurability` lists `dynamic` toolsets as unsupported at run time (`durable_exec/dbos/_durability.py`). Find out
   whether that breaks Viktor's feature pattern under DBOS, and adjust the pattern if so.
2. **Pausing inside a tool call.** Viktor's approval handler waits from inside `HandleDeferredToolCalls`. Confirm that
   `DBOS.recv` works from there, since it must be called from the workflow, not from inside a step.

## Design fixes from the adversarial review

These change the design note and are part of the plan:

1. **Playwright runs inside the browser service, not in the agent.** The harness `PlaywrightBrowser` refuses to run
   with durable execution (`playwright/_capability.py:294`) and cannot launch Chrome through bwrap. The agent's
   browser functions are a thin client that sends actions to the service. Each call is a DBOS step, so a replay never
   clicks twice.
2. **The browser lives as long as the run, not the attempt.** A retry must not close it. The service keys browsers by
   run id and closes them when the run finishes, plus an idle reaper for orphans.
3. **After any browser restart the agent is told** ("browser restarted from saved sign-ins, now at URL X"), and every
   step must be safe to repeat (Mike's rule: check the cart before adding).
4. **Hand-off is requester-only.** In milestone 2 hand-off links go by DM only.
5. **Hand-off links identify the hand-off; identity comes from being signed in.** Reconnecting is allowed; one
   active connection per hand-off.
6. **Live view must pass real mouse down, move and up,** not just clicks, so a human can do press-and-hold, and it
   must follow popups and return to the opener when they close.
7. **Screenshots never go into checkpoints or history directly;** they go through an artifacts table (Viktor's
   `artifacts.attach` pattern).
8. **Purchases and messages sent as the user always need approval,** and each task has an allowed-sites list.
9. **Sign-ins, cookies, hand-off links and page contents are never logged.**

## Order of work

| Step | Work | Done when |
|---|---|---|
| 0 | **Mike: Linux spike.** Chrome in bwrap on a data-centre server, against walmart.com search: headless vs visible vs visible + proxy + returning profile, plus "headless with the giveaways removed"; and a session signed in at home then used from the server | Challenge rates, start time, memory and CPU per Chrome measured; we know whether a moved session survives |
| 1 | **Skeleton.** Copy Viktor's app, settings, db, agent, feature, approvals, memory; replace pgtask with DBOS; Postgres in Docker | A message through the API gets an agent reply; an approval pauses and resumes the workflow; the two DBOS checks above answered |
| 2 | **One-person data model.** Rewrite `models.py`, `store.py`, migrations around users | Tests prove user A cannot read user B's threads, memory, schedules or sign-ins |
| 3 | **Browser service.** One Chrome per run in bwrap, kept across pauses, `BrowserState` in and out, saved sign-ins encrypted per user | The agent can open, click and read in its own Chrome; a pause and resume finds the same Chrome |
| 4 | **Hand-off + Slack.** The hand-off approval, the chosen hand-off mode, Viktor's Slack files | The bot asks us to sign in, we do, it carries on: our own Grok Bot in our Slack |
| 5 | **Schedules.** `DBOS.create_schedule` per user schedule | A weekly Walmart run stays signed in across weeks |
| 6 | **Web app.** Chat, hand-off, signed-in sites, schedules | Usable without Slack |
| 7 | **Consumers.** Sign-up, Discord, limits, per-user keys | Strangers can use it |

## Open questions

- **Which hand-off first: live view, local window, or both?** Depends on step 0's "does a moved session survive".
- **Deploy:** Viktor runs on Kubernetes (Helm, Tilt, kind). Chrome in bwrap with a virtual screen needs user
  namespaces inside the container. Copy Viktor's Kubernetes setup, or start on one server with Docker Compose?
- **Web framework:** Viktor uses Starlette; Mike's design says FastAPI. Copying Viktor means Starlette.
- **Proxy provider and cost per user.**
