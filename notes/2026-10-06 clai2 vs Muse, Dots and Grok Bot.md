# clai2 against Muse, dots and Grok Bot: what to build

**Status:** written 2026-10-06 by Claude for Aditya. Product facts come from vendor pages fetched today, except
OpenAI's dots pages, which returned 403 and are known only from search snippets and press (Android Authority,
The Next Web). clai2 facts come from pydantic-ai `origin/main` at `803d3eb46` (2026-10-06). Not agreed with anyone.

## The three products in one table

| | Meta **Muse** (2026-09-08) | OpenAI **dots** (2026-09-29) | xAI **Grok Bot** (2026-08-11) |
|---|---|---|---|
| Environment | One cloud VM per user; agent in a systemd-nspawn container on Debian; data in Postgres outside it | A cloud computer and browser per dot; can start Codex cloud tasks | One persistent cloud computer per account, shared by all its bots; one screen per bot |
| Computer use | Shell; Chromium through a broker; the browser subagent sees only accessibility-tree snapshots, no JS | Browser and cloud computer | Browser, filesystem, terminal, desktop GUI; connectors first, computer use as fallback |
| Channels | iOS, Android, web, WhatsApp | ChatGPT apps, Slack, Teams; iMessage/RCS waitlist | Desktop and mobile apps; Slack for Team Bots |
| Autonomy | Long-running goals, proactive suggestions, memory | Scheduled tasks, proactive read-only research, memory from connected apps | Routines on schedules or events (Slack, GitHub), up to 50 per bot; bot-to-bot messages; group chats of 2 to 6 bots |
| Control | "Sentinel" policy engine: allow, deny or ask, with one-time, session, task, timed or permanent grants | Built-in and custom rules: allow, needs approval, block; an Activity View to watch and redirect | Approvals for send, buy, delete, publish, production changes |
| Credentials | Vault outside the VM; the agent holds stand-in tokens swapped for real ones at the network boundary; single-use card numbers | 4,000+ app connectors | Shared logins across bots; MCP and Composio connectors |
| Teaching | none | none | "Teach a task": record a browser demo of up to 10 minutes and turn it into a skill |

All three share the same shape. The agent lives in a cloud machine that outlives your laptop. You reach it from
wherever you already talk, it wakes up on its own, and it comes back to you only for decisions.

## Where clai2 stands

clai2 is a terminal process on your machine. The harness already ships most of the parts the products use, but
clai2 wires in almost none of them.

| Area | In the harness on main | Wired into clai2 |
|---|---|---|
| Workspaces | `modal_sandbox`, `e2b_sandbox`, `sprites_sandbox`, `bubblewrap_sandbox`, `ssh_workspace` | No. `coder` runs unsandboxed on the host; Monty is used only in speculative CodeMode; `--worktree` isolates a checkout, not a machine |
| Computer use | `playwright`, `browser_use` | Catalog entries, off |
| Channels | `experimental/acp`; core `ui/ag_ui`, `ui/vercel_ai` | TUI and `-p` headless only. `builtin_plugins/slack.py` gives the agent Slack *tools* acting as you (user token); it is not a way to talk to clai2 |
| Background work | `background_tools`, `SubAgents`; core Temporal/DBOS/Prefect | `/fork` and child tasks (`runtime/tasks.py`) live only as long as the process |
| Schedules, triggers | none | None |
| Approvals | Deferred tools, guardrails | `ask_user` multiple-choice only; no approval UI or policy |

## What to build, in order

Each step depends on the one before it, so the order matters more than the list.

1. **Monty first, a machine only when a process has to run.** See "Monty first" below. When a cloud machine is
   needed, it should be a named workspace that survives quitting: `/workspace attach research` resumes the same
   machine, files and logins. Sprites (sleep when idle, keep files) and SSH fit this best; Modal's 5-minute default
   lifetime does not.
2. **`clai2 serve`: a headless daemon that runs inside that workspace.** This is what turns a CLI into a bot. The
   TUI, Slack and the editor all become clients of one running agent, instead of each owning the agent loop. Runs
   need to survive restarts, so this is where durable execution (Temporal, DBOS or Absurd) comes in.
3. **Inbound channels.** See "Channels" below. A teammate @mentions clai2 in a Slack thread; the harness ACP server
   covers Zed and JetBrains. Both connect to the daemon from step 2. Slack is the channel both dots and Grok Team
   Bots chose for teams.
4. **Approvals that reach you while you are away.** A small policy (allow, ask, deny, per tool, with a grant scope)
   on top of deferred tools, with the "ask" sent as Slack buttons or a phone notification. Without this, an
   unattended agent either stops at every risky step or never stops.
5. **Routines.** Scheduled prompts ("triage new issues every morning") and event triggers (a GitHub webhook, a Slack
   message). Small once steps 2 and 4 exist.
6. **Computer use inside the workspace.** Turn on `playwright` in the sandbox first. Muse's pattern, feeding only
   accessibility-tree snapshots and no page JavaScript, is cheaper and safer than screenshots. A full desktop on
   E2B comes next for GUI-only apps; see "Computer use" below.
7. **Keep real credentials out of the sandbox.** Muse's stand-in-token proxy is the right model for a remote
   workspace: the sandbox holds placeholders, and an egress proxy we control swaps in the real key. This matters as
   soon as step 1 puts the agent on a machine you do not fully trust.
8. **Several named bots.** A roster of bots with roles sharing one workspace, messaging each other through the
   daemon's queue. This is where Grok Bot is ahead of the other two.

## Ideas that would set clai2 apart

- **Open by design.** None of the three is open source or lets you choose the model or the machine. clai2 with a
  pluggable model and a pluggable workspace provider (your SSH box, Modal, Sprites) is the open counterpart.
- **Fork the machine, not just the conversation.** `/fork` already copies history. With Sprites checkpoints, a fork
  could also copy the workspace, so 20 forks try 20 approaches in parallel. Mike described exactly this use of Grok
  Bot in the 2026-09-18 huddle.
- **Logfire as the Activity View.** dots ships a view for watching and redirecting a dot. clai2 already traces to
  Logfire; a live link to the trace from Slack is that view, for free.
- **Teach by demonstration with Playwright codegen.** Record a browser session in the workspace and save it as a
  skill, as Grok Bot does.

## Open questions

- Which workspace provider is the default for a persistent workspace: Sprites or SSH?
- Does `clai2 serve` belong in clai2, or is it a harness-level runner that clai2 and other apps share?
- Which durable backend: Temporal, DBOS, or Absurd, now that the Absurd port is in the monorepo?

## Channels: talking to clai2, or any Pydantic AI agent, from Slack and friends

### What exists

- **Point72's csp-bot** (Apache-2.0, 15 stars, alpha). One process serves Slack, Discord, Symphony and Telegram.
  The platform layer is not csp-bot itself but **chatom** (`1kbgz/chatom`, a personal-org repo, 5 stars), which has
  a unified pydantic `Message`/`User`/`Channel` model and a backend class with capability flags: send, edit, reply in
  thread, react, upload, buttons. Its Pydantic AI `AgentCommand` runs the agent in a 4-thread pool, polls the result
  from the csp graph, posts status as new messages and only the final answer at the end. No streaming, no edits, no
  approval buttons, and sessions are keyed by user and channel rather than thread (15-minute TTL). Douwe recommended
  it and floated bringing it under the Pydantic org (`douwe/2026-09-16.md`); nothing was decided.
- **Viktor** (`~/pydantic_repos/viktor`). Slack Events API, Pydantic AI directly (not clai2), runs on pgtask with
  every model request and tool call checkpointed. It reacts with 👀, posts a placeholder and streams by editing it
  (`viktor/progress.py`, throttled under Slack's rate limit). Approvals end in `DeferredToolRequests`: it posts a
  button card and the task suspends durably until the button is clicked (`viktor/approvals.py`). Its outbound seam
  is a `ChatSurface` protocol (`viktor/surface.py`) with Slack and HTTP implementations.
- **Your own channels PRs**, stranded by the harness move into the monorepo: harness #738 (`ChannelAdapter`,
  `InboundMessage`, `ConversationStore`, `ChannelHost`), #740/#794 Slack, #741 WhatsApp, #802 Telegram, #804 Discord.
  Text only: no streaming, edits or approvals.
- **Pydantic AI seams.** `pydantic_ai/ui` (`UIAdapter`, `UIEventStream`) assumes one HTTP request and response with
  the client holding history; its per-event hooks (`handle_text_delta`, `handle_deferred_tool_requests`, ...) are
  reusable. Harness ACP has the right session and permission ideas (`PermissionPolicy`, "always allow" scopes), but
  waits for approval in-process, which is wrong for a button clicked hours later. clai2's `cli/headless.py` runs one
  turn and prints the answer; it has no event output and no approval path.

### Recommendation

Do not adopt csp-bot as the layer. Its synchronous csp graph is why it polls threads, it has none of streaming,
edits or approvals, and its Pydantic AI value lives in chatom, a dependency we would not own. Copy its good ideas:
one process for several platforms, chatom's capability list as a checklist, an access policy scoped to the invoking
channel and user, tool-call budgets, and versioned history.

Build a harness `channels` module, revived from #738 and ported into the monorepo:

```
Slack / Discord / Telegram webhook
  └─ ChannelAdapter.messages()           inbound: InboundMessage(conversation_id = thread id, ...)
       └─ ChannelHost
            ├─ ConversationStore.load(thread id)       clai2: SqliteConversationStore
            ├─ agent.run(... event_stream_handler)     any Agent, or clai2 through a new non-TTY entry point
            │    └─ events ─> ChatSurface.update()      throttled edits of one placeholder (Viktor's progress.py)
            └─ output is DeferredToolRequests?         <- the run ENDS here, nothing waits in memory
                 └─ ChatSurface.request_approval()      button card
                      ... hours later: button webhook
                      └─ resume with DeferredToolResults
```

- Inbound is #738's `ChannelAdapter`; outbound is Viktor's `ChatSurface` generalised, minus calls, plus capability
  flags for platforms that cannot edit or show buttons (fall back to new messages and "reply yes").
- A thread is a conversation. Approval scopes ("always allow `shell` in this thread") reuse ACP's `PermissionPolicy`.
- Durability (pgtask, Temporal, Absurd) is optional: ending the run at `DeferredToolRequests` already survives a
  restart, because the state is the stored history.
- **Viktor becomes a consumer of this layer, not the layer.** It is a product (one Slack app, Postgres, k8s).
- **clai2 needs one small change:** an entry point beside `run_headless` that takes an event sink and returns
  deferred approvals instead of failing. Then a channel host drives clai2 like any other agent.

## Monty first: a machine only when a process has to run

### What Monty covers

Monty (1.1.0 on main) starts a session in about 0.8 ms from a warm pool, against about 195 ms for a running Docker
container and about 1.5 s for a sandbox service. It runs a Python 3.14 subset with about 19 stdlib modules, no
third-party packages, no processes and no network. Files come only through host mounts: read-only, read-write, or
`OverlayMemory` (writes stay in memory). Execution state snapshots to a few kilobytes at any host call. Full Monty's
server parks idle sessions to object storage and frees the worker.

| Task | In Monty | Needs a real OS |
|---|---|---|
| Read, list, grep, edit files | Yes, through host tools or `pathlib` on the mount | No |
| Small Python (text, JSON, regex, math) | Yes | No |
| Python needing numpy, pydantic, csv, inheritance | No | CPython |
| Tests, git, `uv`, node, compilers | Only through host `shell` | Yes |
| Network, browser | Only through host tools | Host or VM |

So Monty covers the think, read and edit loop. Anything that runs a process is OS work, and in clai2's speculative
mode today that is `shell` running **unsandboxed on the host** (`runtime/speculative_mode.py` folds `shell` in).

### The tiers

```
model writes run_code
  └─ Monty  (0.8 ms, no OS)                       tier 0: reads, edits, glue code, calls tools
       └─ calls shell(...)                        <- the escalation signal: Monty can never run a process
            ├─ default: local OS sandbox          tier 1: Seatbelt on macOS (~6 ms) / bwrap on Linux
            └─ by rule: cloud workspace           tier 2: Sprites / E2B / Modal, created lazily on first use
                 rule = untrusted code, Linux toolchain on a Mac, long or parallel jobs, running headless
```

- **Escalate on the `shell` call, not on Monty failing.** The tier is chosen by rule. Failure-triggered escalation
  only on a sandbox denial (network blocked), with approval. No free-form `escalate` tool to start with.
- **Tier 1 is nearly free.** The tested Seatbelt profile (`aditya/2026-10-02 Sandboxing shell commands.md`) blocks
  writes outside the project, `~/.ssh` and the network at about 6 ms a command. There is no harness wrapper for it
  yet; `BubblewrapSandbox` covers Linux.
- **Tier 2 is already lazy.** The core workspace protocol says a backend does no I/O until its first operation, so
  Modal, E2B and Sprites only boot on the first call, provided `RepoContext` is off (clai2 already ships
  `repo_context: false`). Sprites sleep for free and keep files, so they are the default for a named workspace.
- **One filesystem at a time.** Escalating to a VM hands off the whole run: ship the tree as a git bundle plus the
  uncommitted diff, switch the run's workspace, drop the local mount, and bring changes back as a diff. No two-way
  sync, which also matches the 2026-09-21 ruling against showing the model two filesystems. Anything in Monty's
  overlay or scratch files must be flushed first.
- **Python Monty cannot run** gets a hint to use `shell` with `python`, which follows the same tiers. On Full Monty,
  `monty-cpython` speaks the same protocol, so tools and mounts keep working; variables do not carry over.

### Gaps to fix first

- The harness CodeMode README's "allowed stdlib" list predates Monty 1.1; the model gets outdated guidance.
- Speculative mode runs side effects through `shell` before the run is approved.
- The workspace protocol has no bulk upload, so a per-file handoff of a large repo is too slow; use a bundle.
- Monty's snapshot does not transfer to CPython; only files and returned values cross tiers.

## Computer use: a full desktop on E2B

### What E2B offers

E2B's `desktop` template is Ubuntu 22.04 with Xvfb, Xfce, Firefox, Chrome and VS Code, plus x11vnc and noVNC for a
live view. The `e2b-desktop` SDK (2.6.1) offers `screenshot()`, clicks, `drag`, `scroll`, `write`, `press`,
`launch`, `open`, and `stream.get_url(view_only=..., auth_key=...)` for a browser link to watch the screen. Every
action is an `xdotool` or `scrot` command sent through the ordinary `commands.run`. The SDK is sync only, takes
seconds to start (it waits up to 60 s for Xfce), and loses its desktop state when you reattach with `connect()`.
Pause keeps memory and running apps (about 4 s per GiB to pause, about 1 s to resume). A 2 vCPU / 4 GiB sandbox costs
about $0.165 an hour while running. Sources: docs.e2b.dev/use-cases/computer-use, github.com/e2b-dev/desktop,
e2b.dev/pricing, docs.e2b.dev/sandbox/persistence.

### What we have

- Core Pydantic AI has **no computer-use tool** for Anthropic or OpenAI; `models/openai.py` notes the ComputerUse
  built-in tool is not yet supported.
- Harness `e2b_sandbox` uses plain `e2b.AsyncSandbox`, creates lazily, pauses on timeout, reattaches through a
  `WorkspaceRef` kept in message history, and exposes `get_sandbox()`. So `E2BSandbox(template='desktop')` already
  gives Shell, FileSystem and Coder the same machine the desktop runs on.
- Harness `playwright` already returns screenshots as `BinaryContent` in a `ToolReturn`, the pattern a desktop tool
  should follow.

### Recommendation

```
E2BSandbox(template='desktop')              one machine: shell, files, desktop
  └─ E2BDesktop capability                  started only on the first desktop action
       ├─ ensure Xvfb + Xfce running        idempotent, so pause / resume / reattach work
       ├─ computer(action=...)              screenshot, click, drag, scroll, type, key, open_url, launch
       │    └─ xdotool / scrot via AsyncSandbox.commands.run  -> ToolReturn(PNG BinaryContent)
       ├─ live view URL (view-only + auth)  posted to the clai2 TUI or the Slack thread
       └─ approvals                         launch, open_url off an allowlist, risky key combos
```

- Build on `AsyncSandbox.commands.run`, not the `e2b-desktop` SDK, to stay async and survive reattach.
- One generic `computer` tool that works with every model; map it to Anthropic's and OpenAI's native computer-use
  tools later, which needs a client-executed `ComputerUseTool` in core.
- Cost: last in the Monty-first chain (Monty, then shell and files, then Playwright for web, then the desktop only for
  GUI-only apps). Pause when the run ends or goes idle, cap screenshots per run and downscale them. A desktop
  subagent with its own budget, sharing the sandbox ref, keeps screenshots out of the main conversation.
- Never write the live-view auth key into spans.

## Sessions and takeover: going back and forth

### How the products do it

**Grok Bot** (docs.x.ai/grok-bot, fetched 2026-10-06; the security page says it runs on Cursor-hosted cloud
computers):

- **Takeover.** You open "Agent Computer" from a conversation and watch clicks and typing live. When the bot hits a
  password, passkey, 2FA, CAPTCHA, payment or identity check, it hands over: "Open the computer, take control,
  complete only the blocked step, and tell the Bot to continue." The bot is paused while you drive and resumes on
  "Return control to the Bot". This works on desktop and on mobile.
- **Credentials.** "The Bot hands the computer to the member rather than typing credentials." A separate masked
  secret request "keeps it out of the transcript and away from the model". Browser sessions persist on the
  computer's disk, so you rarely sign in twice. When a session expires, the bot pauses and notifies you.
- **Continuity.** Desktop and mobile share the same bots, conversations and computer; you pick up a thread on either.
  Push notifications cover a result, a question or an approval request.
- **Approvals.** These are inline cards showing the operation and its inputs, with Allow once / Always allow / Deny,
  plus an Auto Review model that screens risky actions.

**Others.** Muse pauses the agent completely during takeover and shows approvals in the client UI, not in the
conversation, so injected text cannot fake consent. dots (search snippets only) has a "Take over" button and four
rule levels: act, act if pre-approved, ask, hand off. ChatGPT agent is the only one that says it captures no
screenshots while you drive.

**Mechanism vendors.** All of these expose an iframe URL with a read-only or interactive mode, and persistent browser
profiles for logins:

- Browserbase, Steel, Hyperbrowser and Anchor work this way. Steel's URLs are unauthenticated, Hyperbrowser's tokens
  last 12 hours, and Anchor offers one-time URLs.
- Kernel can flip read-only at runtime without a reload, which is a clean Take over / Hand back toggle. Its docs warn
  "only one browser can safely write to a profile at a time".
- E2B desktop gives noVNC with an auth key and `view_only=False`.

### What our code has (pydantic-ai main, 803d3eb46)

- **Conversation id.** A conversation is a uuid in `SqliteConversationStore` (`step_persistence/conversations.py`).
  The sandbox travels with it: each `ModelResponse` carries a `WorkspaceRef(provider, id)`, and a later run reattaches
  from the latest one. So loading the same history on another surface already lands in the same VM.
- **Tied to a folder.** clai2 ties a session to a local folder (`Session.workspace` is the cwd, `runtime/_session.py`).
  A sandbox ref is treated as "not stale", so a resumed session goes back into its sandbox.
- **Concurrency.** It is an optimistic `revision` check plus "busy in process N" by pid, which means nothing across
  hosts. **Workspaces have no lock at all**: two runs holding the same ref both act on it.
- **Missing.** Nothing exists for live view, takeover, VNC or preview URLs. ACP has only an in-memory session store.

### Design

**Takeover is an approval with a screen attached.** We already have the machinery for "the run stops, a human does
something, the run resumes", which is deferred tools. Takeover reuses it:

```
agent calls request_takeover(reason="GitHub sign-in needs 2FA")      deferred tool
  └─ run ENDS with DeferredToolRequests          <- the agent is not running at all, so it is blind by construction
       └─ surface shows a card: reason + "Take over" button
            └─ live view link: noVNC / iframe, interactive, short-lived signed URL via our proxy
                 human signs in on the VM; cookies land in the VM's browser profile
            └─ "Return control" button
       └─ resume with DeferredToolResults("user completed sign-in")   no frames, just a summary
```

- **The same card works in every surface.** The TUI opens the link in your browser. Slack posts the card with
  buttons. A phone gets a push notification with a deep link to that thread.
- **Logins persist** because the browser profile lives on the VM's disk: E2B pause keeps it, and Sprites keep files.
  The agent never sees the password. Hand-off is hard-coded for passwords, 2FA and payments; the rest uses rule
  levels: act, ask, hand off.
- **Masked secret requests** are a separate form, never chat. The value goes to the credential store and the model
  gets a placeholder that our egress proxy swaps for the real value (the Muse pattern from step 7).
- **Live-view links** are short-lived and signed, issued by our side. A raw VNC or debug URL hands the machine to
  anyone holding it.

**Sessions move between surfaces by conversation id**, which is already surface-agnostic. Four things are needed:

1. **A bindings table** mapping a surface handle to a conversation id: Slack `channel:thread_ts`, a terminal
   `/resume <id>`, a web URL. `/handoff` in the TUI prints a link; `clai2 --resume <id>` picks up a Slack thread.
2. **A lease instead of the pid check.** Record owner, surface and expiry, renewed by heartbeat, in a store all
   surfaces share (Postgres once there is more than one host). A second surface either queues its message or
   takes over the lease explicitly ("continue here").
3. **A workspace lease.** There is one writer per `WorkspaceRef` and per browser profile. The lease also covers human
   takeover, so the agent cannot act while you drive.
4. **Re-key clai2's session folder** for remote sessions by `WorkspaceRef`, not the local cwd, so a Slack-born session
   opens in the terminal without the "belongs to another folder" check firing.

### Open questions

- Is the live-view proxy ours (one service in front of noVNC or E2B) or the provider's link passed through?
- Per-user computer shared by all bots (Grok Bot) or per-bot computer (dots)? Shared means logins once; per-bot means
  isolation.
- Where does the shared store live for a single developer: SQLite on the VM, or a hosted Postgres from day one?
