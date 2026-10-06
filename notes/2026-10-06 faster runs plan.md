# Faster runs plan — 2026-10-06

Research and bounded implementation for [#47](https://github.com/adtyavrdhn/montybot/issues/47).
This is not a claim that all of #47 is complete. Streaming, model routing and click selection remain open.

## Baseline and scope

- Fetched origin and inspected the trees of **all fetched origin branches before creating this worktree**.
  There were four existing dated notes, but no faster-runs research note on any of those branches.
  Base: `64c90b5` (`origin/main`); isolated branch: `aditya/47-faster-research`.
- The issue reports one run with model calls of 2.1, 5.8 and 2.9 seconds: approximately 80% model,
  15% browser, negligible Monty. That is the issue author's observation, not a new benchmark or a distribution.
- [#49](https://github.com/adtyavrdhn/montybot/pull/49) already enables Anthropic instruction, tool-definition
  and message caching in `montybot/agent.py`. Cache-hit rate and provider latency savings are not measured here.
- Read `agent.py`, `code.py`, `browsing.py`, `browser/snapshot.py`, `browser/snapshot.js`,
  `workflows.py`, `observability.py`, and the relevant existing tests. Host functions already return the
  post-action snapshot. A single snippet supports sequential navigation, search, extraction and computation.
  There is no need for a new action-batch API.
- `run_code` is **one DBOS step**. Completed steps replay their recorded result; an interrupted snippet can
  repeat from its beginning, including actions already performed. Batching is appropriate for read-only or
  safely repeatable work, not a substitute for approval or idempotence checks. The lexical irreversible-click
  guard is not a complete classifier of irreversible actions, and Enter can also submit forms. In particular,
  a numeric ref absent from the returned text has no label for this heuristic to inspect; it does not fail closed.
  Ref preservation inside the walker is not agent-visible completeness or approval safety. The prompt prohibits
  guessed/stale refs, but an enforcement change needs a separate safety investigation; none is made here.
- Browser functions serialize actions on one tab. Do not promise parallel browsing from `asyncio.gather`.

## Browser Use: verified approach, not an API to assume

Source inspected at upstream commit
[`42dfdc1b2b8a691b64d54ee775b8b49e12d7a641`](https://github.com/browser-use/browser-use/tree/42dfdc1b2b8a691b64d54ee775b8b49e12d7a641):

1. [`agent/system_prompts/system_prompt.md`](https://github.com/browser-use/browser-use/blob/42dfdc1b2b8a691b64d54ee775b8b49e12d7a641/browser_use/agent/system_prompts/system_prompt.md),
   action rules and efficiency guidelines: multiple actions execute sequentially; do not predict actions
   unsupported by the current page. Navigation/evaluate terminate a sequence; page changes can interrupt it.
   The autocomplete guidance specifically waits for suggestions rather than blindly chaining Enter.
2. [`agent/service.py`](https://github.com/browser-use/browser-use/blob/42dfdc1b2b8a691b64d54ee775b8b49e12d7a641/browser_use/agent/service.py):
   `max_actions_per_step=5` by default, response actions capped to that setting; `multi_act` has static
   terminating-action flags and runtime URL/focused-target checks. These are Browser Use's safeguards,
   **not capabilities montybot automatically inherits**. Monty code must inspect each returned page.
3. [`dom/serializer/serializer.py`](https://github.com/browser-use/browser-use/blob/42dfdc1b2b8a691b64d54ee775b8b49e12d7a641/browser_use/dom/serializer/serializer.py):
   simplified visible/interactive DOM, special handling for shadow hosts and file inputs, interactive indices,
   filtered visible text and indications of content below the viewport. This is more than an arbitrary character
   cap. Blindly copying viewport-only filtering risks hiding relevant prose or offscreen controls; montybot also
   deliberately shares its DOM walker across Chromium and Servo, rather than adopting a CDP-specific tree.
4. [Documentation index](https://docs.browser-use.com/llms.txt) and
   [product choice guide](https://docs.browser-use.com/cloud/which-product.md) distinguish the Python library,
   CLI and hosted service. Hosted streaming, model, session and browser features are not library API guarantees.
5. The pinned [README](https://github.com/browser-use/browser-use/blob/42dfdc1b2b8a691b64d54ee775b8b49e12d7a641/README.md)
   links [the showcase/demo catalogue](https://browser-use.com/showcase). Reviewed the source/prompt approach and
   demo entry point; **did not execute a hosted demo or reproduce its speed/success claims**. CAPTCHA automation
   advertised by Browser Use is explicitly incompatible with montybot's user hand-off policy.

Decision: adopt the small instruction-level lesson (known sequential work, inspect returned state, stop at decisions),
not a new browser framework, blind action prediction, routing layer or clickable-element classifier.

## Jev / TypeSafe: capabilities and integration evidence

Sources: [API](https://docs.typesafe.ai/api.md), [Choice](https://docs.typesafe.ai/primitives/choice.md),
[Score](https://docs.typesafe.ai/primitives/score.md), [Noul](https://docs.typesafe.ai/primitives/noul.md),
[confidence](https://docs.typesafe.ai/confidence.md), [models](https://docs.typesafe.ai/models.md),
[Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md),
[intent routing pattern](https://docs.typesafe.ai/patterns/intent-routing.md).
These documentation pages are mutable; this note records the inspected contract, not a permanent service guarantee.

- Jev is a **decision model**, not a general text/code-generating model. The HTTP contract evaluates `state`
  with a named `questions` map and model id. Choice selects from supplied options and returns their probabilities;
  Score evaluates supplied rubric levels with probabilities and a weighted score; Noul evaluates a yes/no question
  as a probability. Multiple questions may share one request. Application code decides what to execute.
- Choice probability and API confidence are different. The documented Choice confidence normalizes top probability
  against an even split; high confidence is not correctness. Thresholds need labeled examples and held-out testing.
- Verified installed Pydantic AI **2.54.0** source:
  [`models/typesafe.py`](https://github.com/pydantic/pydantic-ai/blob/v2.54.0/pydantic_ai_slim/pydantic_ai/models/typesafe.py),
  [`profiles/typesafe.py`](https://github.com/pydantic/pydantic-ai/blob/v2.54.0/pydantic_ai_slim/pydantic_ai/profiles/typesafe.py),
  and `models/decision.py`. `TypeSafeModel` uses `typesafe-sdk` / `AsyncTypeSafeClient`; installation requires the
  `typesafe` extra. The profile caps a Choice at 255 options and Score at 10 levels. The profile describes 32k tokens
  for state plus the longest question, and 64k for state plus all questions. Check actual model/version limits before
  implementation; aliases `jev-latest` and `jev-preview` can move. Do not assume arbitrary free-form arguments.
- Viktor's local checkout at **`c1896df3726b62af64d452100819407a10f042fc`**, `viktor/ambient.py` and
  `viktor/settings.py`, is the concrete integration: `Agent(..., output_type=[stay_quiet, React, reply])`,
  an annotated finite emoji choice, `typesafe:jev-latest`, a 10-second timeout, a durable `decide` step,
  and application-controlled quiet/react/full-reply handling. `route_likelihood` reads
  `response.provider_details['route']['probabilities'][choice]`; this is **top probability**, not API confidence.
  Settings recommend pinning a version once thresholds are tuned. Source origin is recorded in montybot's agent
  and workflow headers; this is code evidence, not an online Jev benchmark.
- **Do not copy Viktor's logging verbatim**: its ambient decision log includes `conversation=conversation`.
  Montybot must keep messages, pages, typed text, cookies and hand-off secrets out of traces. Existing
  `include_content=False` / `include_binary_content=False` instrumentation stays unchanged.
- Jev's documented jagged edges include literal scoping/negation, multi-stage indirection, numeric precision and
  unnecessary context. A finite list of candidate refs could be represented as a Choice, but that alone does not
  establish safe or accurate clicking. No verified browser-click API or montybot click evaluation was found.

Decision: **no Jev dependency, routing or clicking in this PR**. A classifier adds an HTTP roundtrip, timeouts,
provider cost and fallback behavior; routing only wins if avoided model time exceeds all of that at acceptable
quality. Before a later proposal, collect a consented/labeled task set, compare fast/strong baselines, calibrate
versioned probability thresholds, include uncertain/unsupported fallback, and test durable replay, isolation,
provider failures, approvals and stale/ambiguous refs. Click selection must never replace `commit` or hand-off.

## Small changes justified now

1. Clarify the existing prompt: actions already return pages; avoid immediate redundant reads; batch only known
   sequential work; inspect each result, preserve source evidence, stop for unknown decisions/input/approval and
   warn about whole-snippet replay. This can reduce model/tool roundtrips, but actual model behavior is unmeasured.
   A later explicit reread is still appropriate for delayed results or a changing page.
2. Match the walker budget to the **existing** agent text limit, 12,000, with one shared constant. Previously the
   walker could return 20,000 units and the app clipped it at 12,000 Python characters, losing the walker's
   omitted-lines/refs notice and sometimes part of a control. New snapshots preserve whole control lines and the
   walker notice. UTF-16 units and Python characters are not identical; the walker bound is conservative for
   non-BMP text. The app's defensive cap remains for non-walker/oversized snapshots.
3. Adversarial review found that complete-line clipping at the lower budget could erase a previously readable
   12k–20k first paragraph. Addressed at rendering: preserve a bounded prefix of an oversized first **prose** line
   with an explicit partial-line notice; never expose a partially rendered control. No semantic ranking, viewport
   filtering, reference renumbering or smaller-than-existing agent budget. Omitted content still requires a future
   retrieval/pagination design; repeatedly reading the same stable page will not reveal it.

Tradeoffs: larger batching units have larger replay scope; prompts are not enforced policy. Less intermediate text
saves transport but does not skip DOM walking or reference assignment. Important content past the cap can still be
missed; do not claim a complete page was read. The new first-line fallback retains useful text but not a full long
paragraph, and control names already have their own existing clipping limits.

## Verification and measurement (local, no Docker / Full Monty)

- Targeted existing test paths: `tests/test_run_code.py`, `tests/test_browsing.py`,
  `tests/snapshot/test_snapshot.py`. New regression coverage executes real local Monty with a fake browser session:
  known open/search/compute in one snippet uses exactly `goto, snapshot, type, enter, snapshot`, prints extracted
  evidence, and a subsequent labelled irreversible click is refused before reaching the fake browser.
  This is capability/guard coverage, **not a model-prompt A/B test**, an end-to-end latency measurement, or proof
  that the existing lexical guard catches all irreversible actions.
- Real Chromium snapshot cases cover first prose lines at 11,900, 12,100, 13,000, 19,000 and 20,000 characters,
  retained text, notices, cap, and refs surviving a larger explicit-budget snapshot. A small-budget control case
  proves budget clipping never exposes a partial control line. Existing small-budget and action/ref tests remain.
  Servo and optional Full Monty tests may skip if unavailable; report skips, do not claim both engines tested locally.
- Inline Playwright experiment: 250 paragraphs (`Row i: ` plus 90 x's), each followed by a More button.
  Original 20k-budget snapshot: **19,877 characters**. 12k-budget snapshot: **11,958 characters**:
  **7,919 fewer characters (39.84%)** between browser and host for this synthetic page. Its notice preserves
  `298 more lines, 149 more refs`; old app truncation discarded the walker notice. This is not a token/latency
  benchmark: agent page input was already capped at 12k; traversal still visits the same DOM.
- File-only Ruff lint/format, Pyright for changed Python files, and `git diff --check`. CI, not a local full suite,
  must run the existing complete suite including replay/isolation/trace tests before merge.
- Final local targeted run: **36 passed, 22 skipped** (20 Servo cases and 2 optional Full Monty cases).
  All changed-file Ruff/Pyright checks and `git diff --check` passed. No Docker or Full Monty was started.
- Independent adversarial source/diff reviews identified the long-first-paragraph regression and then a
  four-digit omission-count notice overflowing its fixed reserve by one character. Both were fixed; the latter
  now reserves the actual notice length and has a 1,000-control real-engine regression. No latency claims were
  upgraded based on these tests.

## Streaming: separate PR, still open

Current workflow uses `agent.run`; the app polls every 1.5 seconds. Installed Pydantic AI 2.54.0
[`DBOSModel`](https://github.com/pydantic/pydantic-ai/blob/v2.54.0/pydantic_ai_slim/pydantic_ai/durable_exec/dbos/_model.py)
verifies an actual streaming integration exists: a request-stream step can call an event handler live, drain the
stream, and record the completed response. The capability's event handler surface is visible in the installed
DBOS integration. This does **not** supply a durable, authenticated browser event feed automatically: completed
model-step replay returns the response without rerunning its in-step handler. An interrupted attempt may have
already emitted partial events. Do not infer exactly-once delivery of tokens from this API.

A separate streaming PR requires a verified public integration API and durable, per-user event storage/cursors;
transaction/replay semantics; authenticated SSE authorization on every reconnect; duplicate/out-of-order handling;
cross-user/thread-switch/logout isolation; restart mid-model/tool, tool failures, provider timeout, disconnect and
terminal failure paths; approvals and hand-off reconciliation; no content in traces. Only safe text/activity should
be rendered, not tool arguments/code/secrets. Partial text is provisional, not the final durable answer. Measure
first-event and completion latency separately. Those flows were **not implemented or tested here**.

## Unanswered requirements / next evidence

No live model A/B latency/quality measurement, cache-hit evaluation, Jev service invocation, hosted Browser Use demo
run, click-selection accuracy study or streaming-flow tests were performed. Therefore #47 stays open. This note and
small prompt/snapshot changes are a bounded increment, not support for speculative routing or clicking. Follow-up
work should record content-free aggregate timing/call counts and success/failure, never pages/messages in traces.
