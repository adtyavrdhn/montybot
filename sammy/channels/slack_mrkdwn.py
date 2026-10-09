"""Slack's text both ways: Markdown to mrkdwn for what Sammy sends, and Slack's escaped message text back to plain text
for what Sammy reads.

mrkdwn is `*bold*`, `_italic_`, `~strike~`, `` `code` ``, fenced blocks, `<url|text>` links and `>` quotes; it has no
headings or lists, so a heading becomes a bold line and a list item a bullet. `&`, `<` and `>` are escaped everywhere
else, as Slack reads `<...>` as a link or mention.
"""

from __future__ import annotations

import re

FENCE = re.compile(r'```[^\n`]*\n?(.*?)```', re.DOTALL)
HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
BULLET = re.compile(r'^(\s*)[-*+]\s+')
QUOTE = re.compile(r'^\s{0,3}>\s?')
INLINE = re.compile(
    r'`(?P<code>[^`\n]+)`'
    r'|\[(?P<label>[^\]\n]+)\]\((?P<url>[a-z][a-z0-9+.-]*:[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)\)'
    r'|\*\*(?P<bold>.+?)\*\*'
    r'|__(?P<bold2>.+?)__'
    r'|~~(?P<strike>.+?)~~'
    r'|(?<![\w*])\*(?![\s*])(?P<italic>.+?)(?<![\s*])\*(?![\w*])'
    r'|(?<![\w_])_(?![\s_])(?P<italic2>.+?)(?<![\s_])_(?![\w_])'
)
SLACK_TOKEN = re.compile(r'<([^<>|\s]+)(?:\|([^<>]*))?>')


def escape(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def unescape(text: str) -> str:
    return text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def to_mrkdwn(markdown: str) -> str:
    """Markdown as Slack shows it. Code is kept as written (but escaped); everything else line by line."""
    out: list[str] = []
    start = 0
    for block in FENCE.finditer(markdown):
        out.append(_lines(markdown[start : block.start()]))
        out.append(f'```\n{escape(block.group(1).strip(chr(10)))}\n```')
        start = block.end()
    out.append(_lines(markdown[start:]))
    return ''.join(out)


def _lines(text: str) -> str:
    return '\n'.join(_line(line) for line in text.split('\n'))


def _line(line: str) -> str:
    if heading := HEADING.match(line):
        plain = heading.group(1).replace('**', '').replace('__', '')
        return f'*{_inline(plain)}*' if plain else ''
    if bullet := BULLET.match(line):
        return f'{bullet.group(1)}\N{BULLET} {_inline(line[bullet.end() :])}'
    if quote := QUOTE.match(line):
        return f'>{_inline(line[quote.end() :])}'
    return _inline(line)


def _inline(text: str) -> str:
    out: list[str] = []
    start = 0
    for found in INLINE.finditer(text):
        out.append(escape(text[start : found.start()]))
        out.append(_token(found))
        start = found.end()
    out.append(escape(text[start:]))
    return ''.join(out)


def _token(found: re.Match[str]) -> str:
    if (code := found.group('code')) is not None:
        return f'`{escape(code)}`'
    if (url := found.group('url')) is not None:
        label = found.group('label').replace('|', '/')
        return f'<{escape(url)}|{escape(label)}>'
    if (bold := found.group('bold') or found.group('bold2')) is not None:
        return f'*{_inline(bold)}*'
    if (strike := found.group('strike')) is not None:
        return f'~{_inline(strike)}~'
    return f'_{_inline(found.group("italic") or found.group("italic2"))}_'


def from_slack(text: str, bot_id: str | None) -> str:
    """A message's text as the user wrote it: the bot's mention taken out, other mentions and channels by name, links
    as their URL (with the label too where it says something else), and Slack's escapes undone."""

    def one(found: re.Match[str]) -> str:
        target, label = found.group(1), found.group(2)
        if target.startswith('@'):
            return '' if target[1:] == bot_id else f'@{label or target[1:]}'
        if target.startswith('#'):
            return f'#{label or target[1:]}'
        if target.startswith('!'):
            return f'@{label or target[1:].split("^")[0]}'
        if target.startswith('mailto:'):
            return target.removeprefix('mailto:')
        url = unescape(target)
        return url if not label or unescape(label) in url else f'{unescape(label)} ({url})'

    return unescape(SLACK_TOKEN.sub(one, text)).strip()
