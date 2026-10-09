"""Markdown as Telegram's MarkdownV2, which is strict: every reserved character outside an entity must be escaped, or
Telegram refuses the whole message. Inside code only a backslash and a backtick are escaped, and inside a link's URL
only a backslash and `)`.

What Sammy writes is converted: fenced and inline code, links, bold, italic, strikethrough, headings (bold), bullets
and quotes. Anything else is plain text, escaped. `plain` undoes the escaping, for a message Telegram still refuses.
"""

from __future__ import annotations

import re

RESERVED = '_*[]()~`>#+-=|{}.!\\'
_PLAIN = re.compile('([' + re.escape(RESERVED) + '])')
_UNESCAPE = re.compile(r'\\([' + re.escape(RESERVED) + '])')
_FENCE = re.compile(r'^```([^\n`]*)\n(.*?)\n?```[ \t]*$', re.DOTALL | re.MULTILINE)
_INLINE = re.compile(
    r'(?P<code>`[^`\n]+`)'
    r'|\[(?P<label>[^\]\n]+)\]\((?P<href>(?:[^()\s]|\([^()\s]*\))+)\)'
    r'|(?P<url>https?://[^\s<>()\[\]]+)'
    r'|\*\*(?P<bold>[^*\n]+?)\*\*'
    r'|__(?P<bold2>[^_\n]+?)__'
    r'|~~(?P<strike>[^~\n]+?)~~'
    r'|(?<![\w*])\*(?P<italic>[^*\s][^*\n]*?)\*(?![\w*])'
    r'|(?<![\w_])_(?P<italic2>[^_\s][^_\n]*?)_(?![\w_])'
)
_HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*#*$')
_BULLET = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_QUOTE = re.compile(r'^>\s?(.*)$')


def escape(text: str) -> str:
    """Plain text, every reserved character escaped."""
    return _PLAIN.sub(r'\\\1', text)


def escape_code(text: str) -> str:
    return text.replace('\\', '\\\\').replace('`', '\\`')


def escape_url(url: str) -> str:
    return url.replace('\\', '\\\\').replace(')', '\\)')


def plain(text: str) -> str:
    """MarkdownV2 back to the text it shows, near enough: the escapes removed."""
    return _UNESCAPE.sub(r'\1', text)


def to_markdown_v2(markdown: str) -> str:
    parts: list[str] = []
    start = 0
    for fence in _FENCE.finditer(markdown):
        parts.append(_lines(markdown[start : fence.start()]))
        language = fence.group(1).strip()
        parts.append(f'```{escape_code(language)}\n{escape_code(fence.group(2))}\n```')
        start = fence.end()
    parts.append(_lines(markdown[start:]))
    return ''.join(parts).strip()


def _lines(text: str) -> str:
    return '\n'.join(_line(line) for line in text.split('\n'))


def _line(line: str) -> str:
    if heading := _HEADING.match(line):
        return f'*{_inline(heading.group(1).replace("**", ""))}*'
    if bullet := _BULLET.match(line):
        return f'{bullet.group(1)}• {_inline(bullet.group(2))}'
    if quote := _QUOTE.match(line):
        return f'>{_inline(quote.group(1))}'
    return _inline(line)


def _inline(text: str) -> str:
    out: list[str] = []
    start = 0
    for match in _INLINE.finditer(text):
        out.append(escape(text[start : match.start()]))
        out.append(_entity(match))
        start = match.end()
    out.append(escape(text[start:]))
    return ''.join(out)


def _entity(match: re.Match[str]) -> str:
    if code := match.group('code'):
        return f'`{escape_code(code[1:-1])}`'
    if label := match.group('label'):
        return f'[{_inline(label)}]({escape_url(match.group("href"))})'
    if url := match.group('url'):
        return escape(url)  # Telegram links it; escaped, its `_` and `.` stay as they are
    if bold := match.group('bold') or match.group('bold2'):
        return f'*{_inline(bold)}*'
    if strike := match.group('strike'):
        return f'~{_inline(strike)}~'
    return f'_{_inline(match.group("italic") or match.group("italic2"))}_'
