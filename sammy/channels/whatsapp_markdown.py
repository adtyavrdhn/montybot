"""Markdown in WhatsApp's own formatting: `*bold*`, `_italic_`, `~strike~`, and code as it is (``` and `).

WhatsApp has no headings or links, so a heading is bold and a link is its text and URL. Code is left untouched.
"""

from __future__ import annotations

import re

_CODE = re.compile(r'(```[^\n`]*\n.*?```|`[^`\n]+`)', re.DOTALL)
_FENCE_LANGUAGE = re.compile(r'^```[^\n`]*\n')
_HEADING = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', re.MULTILINE)
_BOLD = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__')
_ITALIC = re.compile(r'(?<![*\w])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![*\w])')
_STRIKE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~')
_IMAGE_OR_LINK = re.compile(r'!?\[([^\]\n]*)\]\(([^)\s]+)\)')
_BULLET = re.compile(r'^([ \t]*)[*+][ \t]+', re.MULTILINE)
_BOLD_MARK = '\x00'
"""Stands for a bold `*` while single `*` italics are turned into `_`."""


def to_whatsapp(markdown: str) -> str:
    pieces = _CODE.split(markdown)
    return ''.join(_code(piece) if i % 2 else _text(piece) for i, piece in enumerate(pieces))


def _code(piece: str) -> str:
    return _FENCE_LANGUAGE.sub('```\n', piece) if piece.startswith('```') else piece


def _link(match: re.Match[str]) -> str:
    text, url = match.group(1).strip(), match.group(2)
    return url if not text or text == url else f'{text} ({url})'


def _text(text: str) -> str:
    text = _IMAGE_OR_LINK.sub(_link, text)
    text = _BULLET.sub(r'\1- ', text)
    text = _HEADING.sub(lambda m: f'{_BOLD_MARK}{m.group(1).replace("**", "")}{_BOLD_MARK}', text)
    text = _BOLD.sub(lambda m: f'{_BOLD_MARK}{m.group(1) or m.group(2)}{_BOLD_MARK}', text)
    text = _ITALIC.sub(r'_\1_', text)
    text = _STRIKE.sub(r'~\1~', text)
    return text.replace(_BOLD_MARK, '*')
