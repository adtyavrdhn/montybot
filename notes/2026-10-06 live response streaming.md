# #47: live response streaming

This increment follows the read-only research in PR #52 (`aditya/47-faster-research`,
`notes/2026-10-06 faster runs plan.md`). It does not import that branch's prompt or
snapshot changes. The goal is earlier visible feedback, **not reduced model latency**.

## Verified integration (installed Pydantic AI 2.54.0)

Inspected the installed `pydantic_ai/durable_exec/dbos/_durability.py`,
`durable_exec/_base.py`, `durable_exec/_utils.py`, and `models/function.py`.
The public `DBOSDurability(event_stream_handler=..., parallel_execution_mode='sequential')`
capability makes `agent.run` consume a real stream. `BaseDurabilityCapability`'s
`_model_request_stream_operation` calls `capture_event_stream` with the handler
**inside the durable model operation**, then records the completed result/events.
`wrap_run_event_stream` explicitly excludes model-response events from workflow-side
handler dispatch: they already ran live inside the step. Tool events are dispatched
in their own durable handler steps. The older `DBOSModel` adapter also supports
streaming, but the capability path above is the integration used here.

A completed model step returns its recorded response on recovery without rerunning
its live handler. An interrupted model step can retry and emit a new preview. This
is not exactly-once token delivery. The handler replaces the current request's
preview on its first model event; SSE transmits complete replacement snapshots,
not an append-only token feed. Thinking, arguments, tool results, provider metadata
and tool names are not retained; step activity uses fixed generic labels. Only
assistant `TextPart` content is previewed, rendered using DOM text nodes.

Function-only `FunctionModel` fixtures have no streaming implementation: they keep
the nonstream `agent.run` path, with durable status/activity still available over SSE.
No invented tokenization or simulated streaming fallback is used.

## Durable completion and upgrades

The original `montybot.run_thread` workflow and original agent/step names remain
registered for existing in-flight workflows. New runs use `montybot.run_thread_stream`
and a separately named streaming agent so recorded nonstream step sequences do not
change during deployment. Existing queued workflow IDs are still harmless to start
twice. Browser/tool durability, approval boundaries and the final database transaction
are unchanged. `store.lock_finished` prevents duplicate history/final messages if a
finish transaction commits before DBOS records its result.

A preview is never inserted into history. SSE reconciles run status, output, activity
and asks from Postgres on each poll; terminal database state supersedes all partial
text. The UI discards its provisional bubble and reloads authoritative thread history
on completion/failure. Errors use the existing durable failure notice, not exception
messages. Existing thread polling remains a fallback for SSE/network failures and
history/ask reconciliation.

## Transport and isolation

`GET /api/runs/<id>/events` uses the same signed HttpOnly session as other API reads.
Missing and cross-user runs return 404 before opening the stream; unauthenticated
requests return 401. Each connection rechecks the account and ownership while open,
uses no-store/no-buffer headers, and closes after five minutes so automatic reconnect
validates the cookie again. Reconnect ignores cursors and sends a current replacement
preview plus authoritative database state, including already-completed/failed runs.
No durable token backlog is needed. Idle streams send comments; terminal streams close.

Thread switches and logout close EventSource, remove partial text and invalidate
stale callbacks/in-flight refreshes. An EventSource error leaves database polling as
fallback. Session cookies are stateless: server-side logout on another client does
not revoke a previously signed cookie or an already-open connection; this is the
existing authentication limitation, not a new revocation guarantee.

## Operational limitations

One application process is sufficient for this ephemeral feed. Preview memory is
bounded to 128 runs, 24,000 text characters per run, 128 text parts and a one-hour
idle lifetime; completion discards it. Slow clients do not queue tokens or hold a DB
connection. These limits affect previews only, never the authoritative final reply.
After restart/eviction, or on a different worker, partial text may be absent until a
new model request emits events. Completed model steps deliberately do not reemit
previews on replay; durable waiting/terminal state remains available. Multi-worker
preview continuity would need a shared broker/store and is not promised here.
SSE observes snapshots every 750 ms, coalescing model chunks; this is a transport
cadence, not a measured latency improvement. Polling the DB for status is retained
rather than introducing another durable notification subsystem.

No stream content is logged/traced, and no new instrumentation is added. Existing
Pydantic AI instrumentation excludes message/binary content. This includes no
logging of pages, credentials, cookies or hand-off links. Assistant text is private
user data transmitted only to its authenticated owner; it is provisional and can
still be incorrect.

## Verification

Targeted tests check intermediate visibility and replacement behavior, filtering,
bounds, the nonstream FunctionModel path, the durable operation replay boundary,
transport isolation/reconnect/terminal state, and UI thread/logout stale-callback
handling. Real DBOS restart/replay coverage is run in CI's Postgres e2e harness.
Tests assert behavior, not elapsed-time or model-speed improvements. Local checks
are limited to changed files and focused tests; no full local pytest/typecheck,
Docker or Full Monty run. Full repository checks are delegated to CI before merge.

Independent adversarial review found pending-poll/SSE ordering and delayed browser
callback races; both were corrected with view/request-generation guards. Follow-up
review conditionally approved merge with green CI. Further stale refresh-401 and
ask-answer callbacks were guarded too. An inline streaming failure telemetry probe
exported two spans and found none of its prompt, partial-text or exception sentinels.
The real e2e test now includes interrupted model retry, completed-step replay and
failure-after-preview/reconnect. Actual pre-deployment legacy recordings and a
commit-before-DBOS-record crash-window fault injection were not reproduced; legacy
compatibility and idempotent final writes are source/targeted-guard verified, not a
claim of exhaustive upgrade/crash testing. No live-provider or measured latency
benchmark was run.
