"""Text for chat platforms: long replies split at the platform's limit."""

from __future__ import annotations

SEPARATORS = ('\n\n', '\n', ' ')
"""Where a reply is split, best first: between paragraphs, then lines, then words."""


def split(text: str, limit: int) -> list[str]:
    """`text` in parts of at most `limit` characters, cut between paragraphs where it can, then between lines, then
    words, and only then inside a word. No part is empty. Pure, so a replayed delivery splits the same way."""
    if limit < 1:
        raise ValueError('the limit must be at least 1')
    return [part for part in _split(text.strip(), limit, 0) if part]


def _split(text: str, limit: int, level: int) -> list[str]:
    if len(text) <= limit:
        return [text.strip()]
    if level == len(SEPARATORS):
        return [text[start : start + limit].strip() for start in range(0, len(text), limit)]
    separator = SEPARATORS[level]
    parts: list[str] = []
    current = ''
    for piece in text.split(separator):
        joined = f'{current}{separator}{piece}' if current else piece
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        if len(piece) <= limit:
            current = piece
        else:
            parts.extend(_split(piece, limit, level + 1))
            current = ''
    if current:
        parts.append(current)
    return [part.strip() for part in parts]
