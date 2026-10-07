# Jev and Personal Agent Protocol

## Decisions

Jev is optional advice, not a second browser driver. Two tools help the main agent interpret a changed user request
and choose among unambiguous link labels. Neither tool clicks, approves, logs content, nor replaces the model that
writes Monty code. They run in named DBOS steps. Uncertainty, missing metadata, missing keys and provider failure
return to the existing agent. This avoids paying an extra model call on every simple task.

The probability of the chosen field value is compared to the threshold. TypeSafe's separate confidence/sureness
number is not substituted. The initial model alias is for evaluation, not a measured deployment recommendation.
Version and threshold should be pinned after labeled user-path evaluation. No latency or accuracy gain is claimed.

Intent uses up to three user requests, so a changed direction is not interpreted only from the original run prompt.
Navigation uses at most 20 distinct link labels from the current snapshot. Buttons, inputs, form values and page
prose are omitted. Duplicate labels abstain. A ref is advice about that snapshot, not a guarantee of a harmless
click: the agent must verify it, and commit approval and hand-off rules remain in force.

## Browsers and UX

Headless means no visible desktop; it does not eliminate the browser process. On the server we keep the existing
headed Chrome on Xvfb inside bwrap. Xvfb supplies the display Chrome needs and keeps the user's desktop separate.
The web app shows screenshots while the bot works and the interactive live view during hand-off. No Mac window
should appear for hosted users. Local headless is useful for tests, not proof of equivalent bot-check success.
Changing the production engine or display mode needs the same site and user-path tests, not a speed assumption.

Monty executes code, the browser service owns Chrome, and the workspace owns files. These responsibilities stay
separate. Streaming improves visible progress, not model latency. Fewer model calls and smaller snapshots need
outcome checks as well as timings, because losing a control or a piece of information can make tasks slower.

## Personal Agent Protocol

Source: https://sierra.ai/blog/introducing-personal-agent-protocol, read on 2026-10-06.

The announcement says v0.1 and a reference implementation will be published later this month. The linked article
is not a wire specification. We cannot implement or claim conformance from it.

It describes website discovery, guest sessions, user-authorized OAuth read/write access, and routes through
websites, MCP/OpenAPI or business agents. When the specification exists, a direct business route may avoid browser
round trips. It must preserve user identity, scopes, explicit action approval, cancellation and audit boundaries.
Browser cookies are not OAuth grants and must never be repurposed as delegation tokens. We will not invent
endpoints, permission formats or discovery documents now. The browser remains the fallback for other sites.

## Rollout and verification

TYPESAFE_API_KEY and LOGFIRE_TOKEN can be added as repo Actions secrets. Trusted main deployment sends only
nonempty allowlisted keys to the VM through SSH stdin, updates its owner-only env file atomically, and leaves
existing settings intact. Jev is on whenever the key is set; no key means no added tools.

Tests use finite scripted choices, the real TypeSafe response adapter, and Postgres-backed DBOS workflow results.
They cover abstention, missing keys/metadata, duplicate labels, bounded candidates, no click execution, and secret
updates. Live provider latency and real-site success remain unverified until the keys and evaluation tasks exist.
