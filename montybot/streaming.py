"""Bounded, process-local previews, never history or a delivery log.

DBOSDurability calls the handler *inside* model steps. Completed steps do not
reemit events on replay; an interrupted step can restart its preview. Clients
replace snapshots, never append events, and reconcile with durable run status.
Nothing here logs or traces content. A restart loses previews, not replies.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import TypedDict

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from montybot.deps import RunDeps

MAX_RUNS = 128
MAX_TEXT = 24_000
TTL_SECONDS = 3600


class Snapshot(TypedDict):
    revision: int
    text: str
    activity: str


@dataclass(repr=False)
class Preview:
    parts: dict[int, str] = field(default_factory=lambda: dict[int, str]())
    activity: str = 'Preparing response'
    revision: int = 0
    updated: float = field(default_factory=monotonic)


_previews: OrderedDict[str, Preview] = OrderedDict()
_revision = 0
# Recovered async workflows run on DBOS's background loop; SSE reads on Starlette's.
# Protect short in-memory updates only; never hold this lock across an await.
_lock = RLock()


def _prune() -> None:
    cutoff = monotonic() - TTL_SECONDS
    for run_id in list(_previews):
        if _previews[run_id].updated < cutoff:
            del _previews[run_id]
    while len(_previews) > MAX_RUNS:
        _previews.popitem(last=False)


def _touch(run_id: str, preview: Preview) -> None:
    global _revision
    _revision += 1
    preview.revision = _revision
    preview.updated = monotonic()
    _previews[run_id] = preview
    _previews.move_to_end(run_id)
    _prune()


def reset(run_id: str) -> None:
    with _lock:
        _touch(run_id, Preview())


def discard(run_id: str) -> None:
    with _lock:
        _previews.pop(run_id, None)


def snapshot(run_id: str) -> Snapshot:
    with _lock:
        _prune()
        preview = _previews.get(run_id)
        if preview is None:
            return {'revision': 0, 'text': '', 'activity': ''}  # the web app shows the run's activity log
        return {
            'revision': preview.revision,
            'text': '\n'.join(preview.parts[i] for i in sorted(preview.parts))[:MAX_TEXT],
            'activity': preview.activity,
        }


def _text(preview: Preview, index: int, text: str) -> None:
    # Bound stored content as well as the wire snapshot, including number of parts.
    if index not in preview.parts and len(preview.parts) >= 128:
        return
    remaining = MAX_TEXT - sum(len(value) for key, value in preview.parts.items() if key != index)
    if remaining > 0:
        preview.parts[index] = text[:remaining]


async def handler(ctx: RunContext[RunDeps], events: AsyncIterable[AgentStreamEvent]) -> None:
    model_started = False
    async for event in events:
        with _lock:
            run_id = ctx.deps.run_id
            if isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent)):
                if not model_started:
                    # A new request/attempt replaces an old preview, including after a
                    # retry. A completed step's replay never reaches this handler.
                    reset(run_id)
                    model_started = True
                preview = _previews.get(run_id, Preview())
                preview.activity = 'Writing response'
                if isinstance(event, (PartStartEvent, PartEndEvent)):
                    if isinstance(event.part, TextPart):
                        _text(preview, event.index, event.part.content)
                    else:
                        preview.parts.pop(event.index, None)
                elif isinstance(event.delta, TextPartDelta):
                    _text(preview, event.index, preview.parts.get(event.index, '') + event.delta.content_delta)
                _touch(run_id, preview)
            elif isinstance(event, (FunctionToolCallEvent, FunctionToolResultEvent)):
                preview = _previews.get(run_id, Preview())
                preview.activity = ''
                # Blank: the web app then shows the run's own activity log ("Opening example.com"). Never retain tool
                # names, arguments, results, thinking or provider metadata here.
                _touch(run_id, preview)
