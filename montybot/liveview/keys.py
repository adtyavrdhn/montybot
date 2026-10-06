"""DOM `KeyboardEvent.key` names, as `Press` uses them, translated for CDP and for WebDriver."""

from __future__ import annotations

from dataclasses import dataclass

from montybot.browser.contract import Modifier


@dataclass(frozen=True, kw_only=True)
class Key:
    key: str
    """The DOM `key`, such as `Enter` or `a`."""
    code: str
    """The DOM `code`, such as `Enter` or `KeyA`, or empty when unknown."""
    key_code: int
    """The Windows virtual key code CDP wants, or 0 when there is none."""
    text: str
    """What the key types, such as `a`, `\\r` for Enter, or empty for keys that type nothing."""
    webdriver: str
    """The value WebDriver's key actions take: the character itself, or a code point such as U+E007 for Enter."""


_NAMED: dict[str, tuple[str, int, str, str]] = {
    # key: (code, Windows key code, text, WebDriver value)
    'Enter': ('Enter', 13, '\r', '\ue007'),
    'Tab': ('Tab', 9, '', '\ue004'),
    'Backspace': ('Backspace', 8, '', '\ue003'),
    'Escape': ('Escape', 27, '', '\ue00c'),
    'Delete': ('Delete', 46, '', '\ue017'),
    'Insert': ('Insert', 45, '', '\ue016'),
    'ArrowLeft': ('ArrowLeft', 37, '', '\ue012'),
    'ArrowUp': ('ArrowUp', 38, '', '\ue013'),
    'ArrowRight': ('ArrowRight', 39, '', '\ue014'),
    'ArrowDown': ('ArrowDown', 40, '', '\ue015'),
    'Home': ('Home', 36, '', '\ue011'),
    'End': ('End', 35, '', '\ue010'),
    'PageUp': ('PageUp', 33, '', '\ue00e'),
    'PageDown': ('PageDown', 34, '', '\ue00f'),
    'Shift': ('ShiftLeft', 16, '', '\ue008'),
    'Control': ('ControlLeft', 17, '', '\ue009'),
    'Alt': ('AltLeft', 18, '', '\ue00a'),
    'Meta': ('MetaLeft', 91, '', '\ue03d'),
    **{f'F{n}': (f'F{n}', 111 + n, '', chr(0xE030 + n)) for n in range(1, 13)},
}

MODIFIER_BITS: dict[Modifier, int] = {'Alt': 1, 'Control': 2, 'Meta': 4, 'Shift': 8}
"""CDP's `modifiers` bit for each modifier."""


def key_for(key: str) -> Key | None:
    """The key named `key`, or None if it is neither a known named key nor a single character."""
    if key in _NAMED:
        code, key_code, text, webdriver = _NAMED[key]
        return Key(key=key, code=code, key_code=key_code, text=text, webdriver=webdriver)
    if len(key) != 1:
        return None
    if key == '\n':
        return key_for('Enter')
    if key.isascii() and key.isalpha():
        code, key_code = f'Key{key.upper()}', ord(key.upper())
    elif key.isascii() and key.isdigit():
        code, key_code = f'Digit{key}', ord(key)
    elif key == ' ':
        code, key_code = 'Space', 32
    else:
        code, key_code = '', 0
    return Key(key=key, code=code, key_code=key_code, text=key, webdriver=key)
