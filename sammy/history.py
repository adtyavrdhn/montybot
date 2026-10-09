"""Long chats: the oldest messages are summarised instead of dropped, and the agent can still look up their words.

```
run_thread, after run.finish
  compact_history(thread_id)       @DBOS.workflow
    step history.compact           repeated while more than `history_window + history_batch` messages are outside
                                   the summary: the oldest `history_batch` (cut at a user's message) go into it
run.start
  for_run                          the summary, then the messages after it (at most a window and a batch)
the agent's tools
  search_history, read_history     any message of the chat, by keywords or by number, word for word
```

`SummarizingCompaction` (pydantic-ai-harness), driven by `compact_now` between runs, writes the summary: the
earlier summary is updated in place, not summarised again. It reads the stored messages, which keep a note per file
and never its bytes (`sammy.attachments`), with tool results cut short. The summary is stored on the thread, so it
is exported to Logfire only as part of the model's messages, under the same content rules as the rest of them
(`sammy.observability`). A run gets it as a system prompt on its first message: the model's provider caches it with
the instructions between the run's model calls.
"""

from __future__ import annotations

from dataclasses import replace

from dbos import DBOS, StepOptions
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
    TextContent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai_harness.compaction import SummarizingCompaction, compact_now

from sammy.attachments import without_files
from sammy.db import Connection, Pool
from sammy.deps import RunDeps
from sammy.observability import timing
from sammy.resources import current, load_model
from sammy.settings import Settings

SEARCH_LIMIT = 10
READ_LIMIT = 20
TEXT_CHARS = 4_000
"""The most of one message's text that `read_history` and `search_history` give back."""
TOOL_CHARS = 500
"""The most of one tool call or result they give back: page snapshots are long."""
CUT = ' [...]'

SUMMARY_INSTRUCTIONS = 'You summarise long chats so they can be carried on later.'
SUMMARY_PROMPT = """\
The messages below are the oldest part of a long chat between a user and Sammy, an assistant that does things for \
the user on the web. Your summary replaces them, so it must carry everything Sammy needs to carry on.

Write it under these headings, leaving out a heading with nothing under it:

## The user
Who they are, what they want, and the preferences and standing instructions they gave.

## Agreed
What was decided or agreed, and why, so it is not asked again.

## Done
What Sammy did: orders, bookings, messages sent, schedules and files, with exact names, sites, dates, times and \
amounts.

## Open
What is still to do, and questions still unanswered.

Quote names, numbers, dates and amounts exactly. Never write down a password, a code or card details. Keep it under \
2,000 words. Reply with the summary only.

<messages>
{messages}
</messages>"""
SUMMARY_NOTE = (
    'That summarises messages #0 to #{last} of this chat, which are not shown here. `search_history` finds what '
    'they said, and `read_history` reads them word for word.'
)

COMPACT_STEP: StepOptions = {
    'name': 'history.compact',
    'retries_allowed': True,
    'max_attempts': 3,
    'interval_seconds': 10.0,
}


def limit(settings: Settings) -> int:
    """The most messages after the summary a run gets: more only while compaction has not caught up."""
    return settings.history_window + settings.history_batch


# --- storage ---


async def summary_of(connection: Connection, thread_id: str) -> tuple[str, int]:
    """The thread's summary, and the position of its first message not in it."""
    cursor = await connection.execute('SELECT summary, compacted_up_to FROM sammy.threads WHERE id = %s', (thread_id,))
    row = await cursor.fetchone()
    return ('', 0) if row is None else (row['summary'], row['compacted_up_to'])


async def save_summary(connection: Connection, thread_id: str, summary: str, *, was: int, now: int) -> bool:
    """False if another compaction moved the thread on from `was` meanwhile: its summary stands."""
    cursor = await connection.execute(
        'UPDATE sammy.threads SET summary = %s, compacted_up_to = %s WHERE id = %s AND compacted_up_to = %s',
        (summary, now, thread_id, was),
    )
    return cursor.rowcount == 1


async def load_messages(
    connection: Connection, thread_id: str, first: int = 0, last: int | None = None
) -> list[tuple[int, ModelMessage]]:
    """The thread's messages from position `first` to `last` (both included; unset: the end), with positions."""
    cursor = await connection.execute(
        'SELECT position, payload FROM sammy.messages WHERE thread_id = %s AND position >= %s '
        'AND (%s::integer IS NULL OR position <= %s) ORDER BY position',
        (thread_id, first, last, last),
    )
    rows = await cursor.fetchall()
    messages = ModelMessagesTypeAdapter.validate_python([row['payload'] for row in rows])
    return [(row['position'], message) for row, message in zip(rows, messages, strict=True)]


async def search_messages(
    connection: Connection, thread_id: str, query: str, limit: int = SEARCH_LIMIT
) -> list[tuple[int, ModelMessage]]:
    """Messages with any word of `query` in them, the best matches first, as `memory.search_memories` finds."""
    cursor = await connection.execute(
        """
        SELECT position, payload FROM sammy.messages,
            (SELECT replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery AS query) AS any_word,
            LATERAL (SELECT jsonb_to_tsvector('english', payload, '["string"]') AS words) AS words
        WHERE thread_id = %s AND words @@ query
        ORDER BY ts_rank(words, query) DESC, position
        LIMIT %s
        """,
        (query, thread_id, limit),
    )
    rows = await cursor.fetchall()
    messages = ModelMessagesTypeAdapter.validate_python([row['payload'] for row in rows])
    return [(row['position'], message) for row, message in zip(rows, messages, strict=True)]


# --- what a run gets ---


async def for_run(connection: Connection, thread_id: str, settings: Settings) -> list[ModelMessage]:
    """The thread's summary, then its messages after it, starting at a user's message."""
    summary, compacted_up_to = await summary_of(connection, thread_id)
    messages = [message for _, message in await load_messages(connection, thread_id, compacted_up_to)]
    return with_summary(recent(messages, limit(settings)), summary, compacted_up_to)


def with_summary(messages: list[ModelMessage], summary: str, compacted_up_to: int) -> list[ModelMessage]:
    """The summary as the opening system prompt of the first message, where it stays the same for the whole run."""
    if not summary:
        return messages
    part = SystemPromptPart(content=f'{summary}\n\n{SUMMARY_NOTE.format(last=compacted_up_to - 1)}')
    if messages and isinstance(first := messages[0], ModelRequest):
        return [replace(first, parts=[part, *first.parts]), *messages[1:]]
    return [ModelRequest(parts=[part]), *messages]


def turn_starts(history: list[ModelMessage]) -> list[int]:
    """Where each user's message is."""
    return [
        index
        for index, message in enumerate(history)
        if isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts)
    ]


def recent(history: list[ModelMessage], limit: int) -> list[ModelMessage]:
    """About the last `limit` messages, starting at a user's message so no tool call is cut from its return. If the
    last turn alone is longer than `limit`, all of it."""
    starts = turn_starts(history)
    if len(history) <= limit or not starts:
        return history
    within = [index for index in starts if index >= len(history) - limit]
    return history[within[0] if within else starts[-1] :]


# --- compaction ---


def batch_end(messages: list[ModelMessage], window: int, batch: int) -> int | None:
    """Where the oldest batch of `messages` ends: at the first user's message from `batch` on that leaves `window`
    messages after it, else at the first from `batch` on. None while there are no more than `window + batch`."""
    if len(messages) <= window + batch:
        return None
    starts = [index for index in turn_starts(messages) if index >= batch]
    fits = [index for index in starts if index <= len(messages) - window]
    return fits[0] if fits else starts[0] if starts else None


async def summarize(model: Model | str, previous: str, messages: list[ModelMessage], max_chars: int) -> str:
    """`previous` updated with what `messages` say, at most `max_chars` long."""
    strategy: SummarizingCompaction[None] = SummarizingCompaction(
        model=model,
        max_messages=1,  # `compact_now` compacts whatever the size
        keep_messages=0,
        preserve_first_user_message=False,
        summary_prompt=SUMMARY_PROMPT,
        instructions=SUMMARY_INSTRUCTIONS,
    )
    # The previous summary, as `SummarizingCompaction` wrote it, is the anchor it updates.
    anchor: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(content=previous)])] if previous else []
    compacted = await compact_now(strategy, [*anchor, *without_files(messages)], model=model)
    summary = compacted[0].parts[-1]
    assert isinstance(summary, SystemPromptPart), 'SummarizingCompaction puts its summary first'
    return cut(summary.content, max_chars)


async def compact_once(pool: Pool, settings: Settings, thread_id: str, model: Model | str) -> bool:
    """Put the oldest batch of the thread's messages outside its summary into it, if there are more than a window
    and a batch. True if it did, so there may be another batch."""
    with timing('history.compact') as span:
        async with pool.connection() as connection:
            previous, done = await summary_of(connection, thread_id)
            messages = [message for _, message in await load_messages(connection, thread_id, done)]
        end = batch_end(messages, settings.history_window, settings.history_batch)
        span.set_attributes({'thread_id': thread_id, 'messages': len(messages), 'batch': end or 0})
        if end is None:
            return False
        summary = await summarize(model, previous, messages[:end], settings.history_summary_chars)
        async with pool.connection() as connection:
            await save_summary(connection, thread_id, summary, was=done, now=done + end)
        return True


@DBOS.workflow(name='sammy.compact_history')
async def compact_history(thread_id: str) -> int:
    """Summarise the thread's oldest messages a batch per step until no more than a window and a batch are left
    outside the summary. A run's workflow starts it once the run is done; how many batches it summarised."""
    resources = current()
    settings = resources.settings
    model = load_model(settings.history_summary_model or settings.model)
    batches = 0
    while await DBOS.run_step_async(COMPACT_STEP, compact_once, resources.pool, settings, thread_id, model):
        batches += 1
    return batches


# --- the agent's tools ---


def cut(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - len(CUT)] + CUT


def render(position: int, message: ModelMessage) -> str:
    """What the user and Sammy said in the message, and the tools Sammy used, as text: never a file's bytes."""
    lines: list[str] = []
    for part in message.parts:
        if isinstance(part, UserPromptPart):
            items = [part.content] if isinstance(part.content, str) else part.content
            text = ' '.join(i if isinstance(i, str) else i.content for i in items if isinstance(i, str | TextContent))
            lines.append(f'#{position} user: {cut(text, TEXT_CHARS)}')
        elif isinstance(part, TextPart):
            lines.append(f'#{position} sammy: {cut(part.content, TEXT_CHARS)}')
        elif isinstance(part, ToolCallPart):
            lines.append(f'#{position} sammy used {part.tool_name}: {cut(part.args_as_json_str(), TOOL_CHARS)}')
        elif isinstance(part, ToolReturnPart):
            lines.append(f'#{position} {part.tool_name} returned: {cut(part.model_response_str(), TOOL_CHARS)}')
    return '\n'.join(lines)


history_tools: FunctionToolset[RunDeps] = FunctionToolset(id='history')


@history_tools.tool
async def search_history(ctx: RunContext[RunDeps], query: str) -> list[str]:
    """Search every message of this chat by keywords, such as 'party venue'; any word may match. Use it when the user
    refers to something said earlier that you cannot see or that the summary does not quote. Each line starts with
    its message number (`#12`) for `read_history`."""
    thread_id = ctx.deps.run.thread_id

    async def step() -> list[str]:
        async with current().pool.connection() as connection:
            found = await search_messages(connection, thread_id, query)
        return [text for position, message in found if (text := render(position, message))]

    return await DBOS.run_step_async({'name': 'history.search'}, step)


@history_tools.tool
async def read_history(ctx: RunContext[RunDeps], start: int, end: int) -> str:
    """Read this chat's messages word for word, from number `start` to `end`, both included (#0 is the first; at
    most 20 at a time). Long texts and tool results are cut short."""
    thread_id = ctx.deps.run.thread_id

    async def step() -> str:
        async with current().pool.connection() as connection:
            found = await load_messages(connection, thread_id, max(start, 0), min(end, start + READ_LIMIT - 1))
        return '\n'.join(text for position, message in found if (text := render(position, message)))

    return await DBOS.run_step_async({'name': 'history.read'}, step) or 'There are no messages with those numbers.'
