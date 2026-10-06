# Browser, isolation and state flow for montybot

**Status:** written 2026-10-06 by Claude for Aditya. Engine facts were checked against each project's repo and docs
on 2026-10-06; nothing was measured locally and no live logins were tried. Builds on
[`2026-10-06 clai2 vs Muse, Dots and Grok Bot.md`](<2026-10-06 clai2 vs Muse, Dots and Grok Bot.md>).

## Starting points agreed in chat

- Monty is the product. As much work as possible stays in Monty as host tools; a machine is the rare fallback.
- For these bots, a machine is needed mostly for a **browser**. If the browser is a tool Monty calls, most bots never
  need a VM.
- What must work: many users talking to the same bots without sharing logins or storage, users taking over the
  browser to sign in, and browser state flowing back and forth between the user and the bot.

## Could Monty have its own browser?

| | Ladybird | Lightpanda | Servo |
|---|---|---|---|
| What | Independent browser, C++ moving to Rust | Headless browser built for agents, Zig + V8 | Browser engine in Rust, embeddable crate |
| Maturity | Pre-alpha; alpha "2026" not shipped | 1.0.0 on 2026-10-02 | `servo` crate 0.7.0 on 2026-10-05; API changes every release |
| License | BSD-2 | **AGPL-3.0**, telemetry on by default | MPL-2.0 |
| Driven by | W3C WebDriver; Firefox DevTools protocol, **no CDP** | **CDP** (Playwright works), WebDriver BiDi, built-in MCP | Rust API (input events, JS, screenshots, AccessKit tree), WebDriver |
| Accessibility tree | Internal only | Yes: CDP `Accessibility.getFullAXTree`, `--dump semantic_tree` | Yes, through AccessKit |
| Real screenshots | Yes | **No**: text-only rendering | Yes, software rendering headless |
| Human takeover | Only by running its Qt UI | No UI to hand over | Possible, since it really renders |
| Logged-in, anti-bot sites | Weakest | Weak: its fingerprint is not Chrome's | Poor to medium |
| Their cost claim | none | 100 pages: 123 MB and 5 s vs 2 GB and 46 s for headless Chrome | none published |

**Verdict.**

- **Lightpanda is usable now** as the cheap tier for reading and clicking through accessibility snapshots. It runs
  as a sidecar process that the harness already knows how to reach: `playwright` takes a `cdp_url`. The AGPL needs a
  legal check before we offer it as a service.
- **Servo is the "Monty's own browser" bet.** It is the only engine that links into a Rust process and renders real
  pixels, so it could serve both the agent and a human takeover. The API is young and site compatibility is low, so
  it is a research track, not the first build.
- **Ladybird is not usable in 2026.** It has no CDP and no embedding API. Watch it after its alpha.
- **None of the three passes Google sign-in or Cloudflare reliably.** Real Chromium stays the fallback for
  sign-in, takeover and anti-bot sites.

## Two browser tiers behind Monty

```
Monty run_code (user A)
  └─ browser_snapshot() / browser_click(ref)          host tools; Monty never touches the browser
       ├─ B0: Lightpanda, one process per (user, run)  ms start, tens of MB; a11y tree, no pixels
       │     escalate when: login page, CAPTCHA, blocked, needs a screenshot, site on the "needs Chrome" list
       └─ B1: Chromium, one process per (user, run)    in a container or gVisor; real pixels
             └─ live view + takeover (CDP Page.startScreencast)
```

Both tiers load the same per-user browser state (next section), so escalating from B0 to B1 keeps the user signed in.

## Browser state: what flows and where

The unit of state is a **browser identity**: one user's cookies and localStorage. It is the JSON that Playwright's
`storage_state` exports, encrypted with a per-user key and versioned.

```
1. bot hits a login page on B0 → escalates to B1 with A's identity → request_takeover (run ends)
2. A opens the live view, signs in (password, 2FA) on B1          the model sees nothing: no run is active
3. A clicks "Return control" → host exports storage_state from B1 → saves identity(A), version n+1
4. run resumes; later runs load identity(A) into B0 (cheap) or B1 (when needed)
5. at the end of every run, the host saves cookies back, under A's lease, so refreshed sessions persist
```

- **Cookies may not survive moving to a different engine.** Some sites tie a session to the browser fingerprint or
  the IP address. Keep each user's egress IP stable through our proxy, and when B0 finds itself signed out with
  identity(A) loaded, retry on B1 before asking A to sign in again.
- **One writer per identity.** A lease on identity(A) stops two runs, or a run and a takeover, from saving over each
  other. This is the workspace lease from the first note, applied to browser state.
- **Untested:** importing a `storage_state` into Lightpanda. Its CDP has `Storage.setCookies`, which Playwright uses,
  so cookies should work; localStorage would go in through `addScriptToEvaluateOnNewDocument`.

## Isolation when many users share a bot

The rule everything else follows from: **the bot acts with the identity of the human who asked, not its own.** In a
shared Slack channel, Alice's request runs with identity(Alice) and Bob's with identity(Bob). There is no team-wide
login unless someone deliberately sets up a shared service account.

What that means in practice:

1. **A browser process per (user, run), never shared.** No browser context is shared across users. A pool keeps only
   *clean* warm processes. A used process is killed, never handed to the next user, because a context reset does not
   clear everything (cache, service workers, HTTP connection state).
2. **Storage keyed by user.** Browser identities, downloaded files and Monty's scratch files (`OverlayMemory`, per
   run) are stored under the user, encrypted with that user's key. Memories are per user; skills can be shared, as
   in Grok's Team Bots.
3. **Output follows the identity, not the channel.** If the bot read Alice's inbox with Alice's cookies, posting the
   answer in a channel shows Alice's data to Bob. A reply produced with a user's private identity goes to that user
   privately (a Slack ephemeral message or DM), unless they approve posting it. Viktor's `ChatSurface` already has
   `post_privately`.
4. **Approvals and takeover go to the identity owner.** Only Alice can approve actions taken as Alice or take over
   her browser. Live-view links are signed for one user and expire.
5. **Leases are per user identity and per workspace,** so concurrent requests from the same user queue up instead of
   racing.

## What is still a machine

- Native desktop apps (no web version): the E2B desktop or a microVM.
- `shell` commands such as `git`, `pytest` or `uv`: the OS tier from the first note.
- Chromium itself (B1) runs in a container or gVisor, not a VM, but it is still the expensive tier. Measure how often
  B0 is enough before sizing B1.

## Next experiments

1. Drive Lightpanda from the harness `playwright` capability through `cdp_url`, on ten sites the bots would use.
   Record which ones work on B0.
2. Export a signed-in `storage_state` from Chromium and import it into Lightpanda. Check whether the sites still see
   the user as signed in.
3. Build a minimal Servo spike: embed the `servo` crate, load a page, read the AccessKit tree, take a screenshot. This
   tells us whether "Monty's own browser" is months or years away.
4. Get the AGPL question on Lightpanda answered before anything ships.
