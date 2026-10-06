# monty-bot: plan and browser design

**Status:** written 2026-10-06 by Claude for Aditya, after a goal-alignment session. It builds on Mike's
[`DESIGN.md`](../DESIGN.md) and the two earlier notes. Numbers marked *measured* come from the spikes in `spikes/`, run on
an M-series Mac on 2026-10-06. Nothing has run on Linux yet. Nothing here is agreed with Mike or Viktor's owner.

## What we are building

monty-bot is our own Grok Bot: a personal bot that does things for people, mostly in a browser. The benchmark is
OpenClaw or Hermes Agent, but hosted, always on, and with a browser that works on real sites.

Decisions from the session:

| # | Question | Decision |
|---|---|---|
| 1 | Who is it for? | Anyone who would use Grok Bot or Muse. A consumer product, not a developer product |
| 2 | Who runs it? | We host everything: the agent loop, Monty, browsers, schedules, storage. Users only see the interface |
| 3 | What is the interface? | Our own web app first (chat next to a live view of the bot's browser), plus Slack and Discord |
| 4 | What do we build on? | Viktor's agent loop. It is durable, multi-user, and already runs Monty through `CodeMode` |
| 5 | Which browser engine? | Chrome. Servo and five other engines were measured and ruled out (see "Why Chrome") |
| 6 | How is Chrome run? | A normal, visible Chrome on a virtual screen, one per user run, in bwrap. No VM |
| 7 | Where does it run first? | One Linux server. More servers only when one is full |

What changes from Mike's `DESIGN.md`:

- **Channels are back.** Mike wrote "agents are the clients". With a consumer product, the bot is the agent people
  talk to, through the web app, Slack or Discord.
- **pgtask instead of DBOS,** because Viktor already runs on pgtask. This needs Mike's agreement.
- **Chrome is visible, not headless,** because of Mike's concern about bot checks (see "Bot checks").
- **A small browser service owns the Chromes,** not the agent run (see "Why a browser service").

Everything else in his design stands: Monty runs the agent's code and reaches the browser only through host functions,
Chrome runs in bwrap with its debugging connection on a pipe, sign-ins are saved encrypted in Postgres, and the user
takes over through a live view.

## The picture: one server

```
our Linux server
  ├─ Viktor web process        web app, Slack and Discord events, the live-view page
  ├─ Viktor worker processes   the agent loop; Monty runs inside them
  ├─ browser service           starts, holds and stops every Chrome on this server
  │    ├─ Chrome for alice     visible Chrome on its own virtual screen, in bwrap
  │    └─ Chrome for bob       a separate Chrome, separate screen, separate bwrap
  └─ Postgres                  chats, schedules, saved sign-ins, which Chrome belongs to which run
```

- **Where are the browsers?** They are ordinary Chrome programs running on our server. Nobody looks at the server's
  screen, so each Chrome draws on a virtual screen (Xvfb), a small program that pretends to be a monitor.
- **Who controls them?** The agent, through the browser service. The user, through a web page that shows a live
  picture of that Chrome's screen.
- **What is saved?** Each user's sign-ins (cookies and site storage), encrypted in Postgres. A Chrome only lives for
  one run; the saved sign-ins live as long as the user.

## Why a browser service

Viktor pauses a run that waits for a person by ending it (`task.wait_for_signal` in `viktor/approvals.py`), and resumes
it later in whichever worker process is free. If the worker that started Chrome owned it, the Chrome would be lost or
unreachable when the run resumes. So one long-lived process, the browser service, owns every Chrome. Workers and the
web page ask it for "alice's browser for run 42" and always get the same Chrome back.

## How a run uses the browser

1. A schedule fires or alice sends a message. Viktor starts the run in a worker.
2. The agent writes Python in Monty. Its first browser call (`goto("https://walmart.com")`) asks the browser service
   for alice's browser for this run.
3. The browser service starts a Chrome for alice with her saved sign-ins, on a fresh virtual screen, inside bwrap.
4. Each `goto`, `click`, `type` or `snapshot` from Monty goes to the browser service, which performs it on alice's
   Chrome and returns the result. Monty never touches Chrome directly.
5. When the run ends, the browser service saves alice's sign-ins to Postgres and closes her Chrome. A Chrome is never
   reused for anyone else.

## How the user takes over the same browser

1. The agent hits Walmart's sign-in page and calls `ask_user("Please sign in to Walmart")`.
2. Viktor sends alice a link (web app, Slack or Discord) and pauses the run. **Alice's Chrome keeps running**, still on
   the sign-in page.
3. Alice opens the link. The web page checks that she is alice, then shows a live picture of her Chrome's screen. Her
   clicks and typing are passed into that same Chrome. The agent waits while she drives.
4. She signs in, does the 2FA, and presses "Return control". The browser service saves her sign-ins.
5. Viktor resumes the run. The agent asks the browser service for alice's browser for this run and gets the same Chrome,
   signed in, on the page she left it on.

If alice does not open the link within N minutes (start with 10), the browser service saves her sign-ins and closes
Chrome to free memory. When she opens the link later, a fresh Chrome starts from her saved sign-ins on the last page.
The page is reloaded, not identical, which is fine for signing in. The agent is told the browser restarted.

`spikes/takeover/` does steps 3 and 4 today with Chrome on a Mac: a one-time link, a live picture, clicks and typing
passed through, and sign-ins saved so a fresh Chrome comes back signed in. Its self-test passes.

## Bot checks

Mike's concern is right: headless Chrome gives itself away. *Measured:* it sends `HeadlessChrome/153` as its user agent,
and every Playwright-launched browser reports `navigator.webdriver = true`. Walmart's search page blocked every engine
except Lightpanda, which only got through because it does not run iframes or workers.

Four giveaways, four fixes:

| Giveaway | Fix |
|---|---|
| "I am headless" | Visible Chrome on a virtual screen |
| "I am automated" | Launch Chrome without the automation flag (`--enable-automation`). To verify in the spike |
| "Brand-new visitor every time" | Reuse each user's saved sign-ins and cookies, so the site sees a returning browser |
| "Coming from a data center" | Send traffic through a residential or ISP proxy, one stable address per user |

When a check still appears (Walmart's press-and-hold), the user takes over and does it, as Grok Bot and ChatGPT agent
do. Passing it usually leaves a cookie, which is saved with the user's sign-ins.

If a site still blocks us, plan B is a managed browser service (Browserbase or Kernel) for that site only, about $0.10
per browser-hour. Plan C, later, is a browser extension that uses the user's own Chrome, as Manus does.

## Speed

Mike is worried it will be slow. What we know:

| Step | Time | Source |
|---|---|---|
| Monty session start | about 1 ms | Monty benchmarks |
| Chrome start (headless shell) | 0.06 s | *measured* |
| Chrome start (full Chrome, headless) | 0.16 s | *measured* |
| Page load: example.com / github.com/login / walmart.com | 0.3 / 0.65 / 2.4 s | *measured*, headless shell |
| One agent step (the model deciding what to click) | several seconds | typical model latency, not measured here |
| Visible Chrome + virtual screen start | unknown | not measured; Linux spike |
| Live picture lag during takeover | unknown on a real network | spike only ran on localhost |

- **The model is the slow part.** Starting Chrome costs a fraction of a second; each model step costs seconds. A
  Walmart cart run is dominated by model steps, not by the browser.
- **The live picture** sends a JPEG of the screen each time it changes. That is fine for typing a password or tapping a
  button. If it feels laggy over real networks, the faster option is streaming the screen as video over WebRTC, which
  Kernel does for its browsers.
- **To settle it,** the Linux spike measures visible-Chrome start time, memory, and live-picture lag from a phone.

We should ask Mike which part he expects to be slow, so the spike measures that.

## Isolation between users

- **One Chrome per user run,** never shared. *Measured:* an extra isolated profile inside an existing Chrome costs about
  20 MiB, a new Chrome about 50 MiB or more. We pay for separate Chromes anyway: a shared Chrome means a shared crash,
  shared cache, and one browser exploit reaching everyone in it.
- **Each Chrome in its own bwrap,** with its own profile folder, `/tmp`, processes and network, per Mike's script.
- **Tools are bound to the run.** No browser function takes a user id; the agent can only reach its own run's Chrome.
- **Takeover links** open only for the user who owns the browser, and work once.
- **One run per user's browser at a time.** A second run of alice's waits, so two runs never overwrite her sign-ins.
- **Never log** sign-ins, cookies, or takeover links.

## Why Chrome

Seven engines measured on the same Mac (details in `spikes/engines/README.md` and `spikes/servo/README.md`):

| Engine | Memory: example / github / walmart | Agent can read forms | Saved sign-ins work | Live picture |
|---|---|---|---|---|
| Chrome headless shell | 64 / 123 / 314 MiB | yes | yes | yes |
| Chrome (full, headless) | 549 / 606 / 882 MiB | yes | yes | yes |
| Firefox | 384 / 467 / 655 MiB | yes | yes | Playwright only |
| WebKit | 187 / 275 / 474 MiB | yes | yes | Playwright only |
| Lightpanda | 10 / 39 / 114 MiB | yes | import broken | none |
| Camoufox | up to 1.8 GiB | yes | yes | Playwright only |
| Servo | 308 / 399 / 505-674 MiB | **no** | cookies only | build it ourselves |

Chrome is the only engine that needs no work for the agent, takeover and saved sign-ins, and every product we looked at
(Browserbase, Steel, Kernel, Cloudflare, ChatGPT agent, Grok Bot) uses it. Visible Chrome on Linux has not been
measured; the Mac's full-Chrome number includes a graphics process and is likely higher than Linux.

## What we use from Viktor

| Viktor part | Gives us |
|---|---|
| Run loop (`handlers.execute`, `PGTaskDurability`) | Durable runs that survive crashes |
| Approvals (`approvals.py`) | Pause and resume. `ask_user` becomes a second kind of approval |
| Schedules (`scheduler.py`) | "Every Tuesday, cart my shopping list" |
| Memory, skills | Per-user memory and reusable skills |
| `Feature` (`feature.py`) | The plug-in shape the browser goes into |
| `CodeMode` in `gateway.py` | Monty running the agent's code |
| Connectors (Composio) | "Check my messages" without us holding tokens |
| Requester rules, `RunDeps` | Authority and private results follow whoever asked |
| `api.py` + `APISurface` | The web app sits on this |
| `SlackSurface`, metering, deploy | A channel, spend caps, and a way to run it |

To adapt: Viktor's tenant is a team's Slack workspace; a consumer needs a personal workspace, sign-up and login. The
agent's instructions say "AI employee in Slack". Not used for v1: Viktor's per-run Kubernetes sandbox Pod.

## What we build

1. **Browser service.** Starts visible Chrome on a virtual screen in bwrap per user run, holds it across pauses,
   performs browser actions, streams the live picture, saves sign-ins. Grows out of `spikes/takeover/takeover.py`.
2. **Browser feature in Viktor.** `goto`, `click`, `type`, `snapshot`, `screenshot` as Monty functions through
   `CodeMode`, plus `ask_user(reason)`.
3. **Takeover as an approval.** `ask_user` uses Viktor's approval path; a new `ChatSurface.request_takeover` posts the
   link; "Return control" resumes the run.
4. **Saved sign-ins.** A Postgres table, one row per user, encrypted per user, one writer at a time.
5. **Web app.** Chat, the live picture next to it, "sites you are signed into", schedules.
6. **Consumer accounts.** Sign-up, a personal workspace per user, connecting Slack or Discord.

## Milestones

1. **Linux spike (Mike).** One Linux server in a data center: visible Chrome on a virtual screen in bwrap, debugging
   connection on a pipe, network through `pasta`. Load walmart.com search and a few target sites three ways (headless;
   visible; visible plus proxy plus a returning profile). Record how often each is challenged, start time, memory, and
   live-picture lag from a phone.
2. **Browser in Viktor, in our own Slack.** Items 1 to 3 on that server. The bot browses, asks us to sign in, we take
   over through the link. This is a working Grok Bot for us.
3. **Schedules and saved sign-ins.** The Walmart Tuesday run end to end, signed in across runs.
4. **Web app.** Chat plus live picture plus sign-ins page.
5. **Consumers.** Sign-up, personal workspaces, Discord, limits.
6. **Hard sites.** Managed browser as a second backend, chosen per site.

## Later: more than one server

One server holds an estimated 50 to 100 Chromes at once (not measured on Linux). When it is full, add identical
servers. Each Chrome lives on the server that started it, and Postgres records which. A request for alice's browser
that reaches another server is forwarded to the right one. If a server dies, the next request starts a new Chrome
elsewhere from her saved sign-ins.

## Open questions

- **Mike:** pgtask instead of DBOS; which part he expects to be slow; does the Linux spike plan above cover his concerns.
- **Viktor's owner:** build monty-bot as Features inside Viktor, or pull Viktor's core into a shared library both use.
- **Takeover gaps:** pasting from a password manager, file uploads, passkeys.
- **Saved sign-ins miss** sessionStorage and service workers; some sites may ask the user to sign in again.
- **Ubuntu 24.04** blocks unprivileged user namespaces by default (AppArmor), which bwrap and Chrome's own sandbox need.
  The fix is known; the server image must include it.
- **Proxy provider and cost** per user.
