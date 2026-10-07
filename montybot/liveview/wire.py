"""The live view's WebSocket messages, in both directions.

Text messages are JSON objects with a `kind`. The user's input uses the contract's action names (`mouse_down`,
`press` and so on) with points flattened to `x` and `y`. Frames are binary messages: a 4-byte big-endian length, a
JSON header of that length (`seq`, `width`, `height`, `mime`), then the image.

    page -> server   mouse_down {x, y, button}   mouse_move {x, y}   mouse_up {x, y, button}   click {x, y}
                     type {text}   press {key, modifiers}   scroll {delta_x, delta_y, x?, y?}
                     switch_tab {tab_id}   viewport {width, height}   give_back {}
    server -> page   hello {handoff_id, reason}   tabs {tabs: [{tab_id, url, title, active}]}   error {message}
                     ended {given_back}   and binary frames
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Literal, cast, get_args

from montybot.browser.contract import (
    Click,
    Modifier,
    MouseButton,
    MouseDown,
    MouseMove,
    MouseUp,
    Point,
    Press,
    Scroll,
    Type,
)
from montybot.browser.live import Frame, LiveInput, Tab, Tabs

MAX_MESSAGE = 64 * 1024
"""The longest text message the server accepts from the page, in characters."""
_MIMES = ('image/jpeg', 'image/png')
_SIZES = (100, 10_000)
"""The smallest and largest `viewport` width and height, in CSS pixels."""


class WireError(ValueError):
    """A message that does not follow this protocol."""


@dataclass(frozen=True, kw_only=True)
class SwitchTab:
    tab_id: str
    kind: Literal['switch_tab'] = 'switch_tab'


@dataclass(frozen=True, kw_only=True)
class ViewportSize:
    """The room the page has for the picture, in CSS pixels. Sent when the page connects and when it resizes."""

    width: int
    height: int
    kind: Literal['viewport'] = 'viewport'


@dataclass(frozen=True, kw_only=True)
class GiveBackRequest:
    """The user pressed "Give back to the bot"."""

    kind: Literal['give_back'] = 'give_back'


ClientMessage = LiveInput | SwitchTab | ViewportSize | GiveBackRequest


@dataclass(frozen=True, kw_only=True)
class Hello:
    handoff_id: str
    reason: str
    kind: Literal['hello'] = 'hello'


@dataclass(frozen=True, kw_only=True)
class ErrorMessage:
    """An input failed, for example a key the engine does not know. Safe to show: never contains typed text."""

    message: str
    kind: Literal['error'] = 'error'


@dataclass(frozen=True, kw_only=True)
class Ended:
    """The hand-off is over. `given_back` is True when this connection gave it back."""

    given_back: bool
    kind: Literal['ended'] = 'ended'


ServerMessage = Hello | Tabs | ErrorMessage | Ended


@dataclass(frozen=True, kw_only=True)
class NumberedFrame:
    seq: int
    """Counts frames on one connection, from 1."""
    frame: Frame


# --- the page's messages ---


def decode_client(text: str) -> ClientMessage:
    if len(text) > MAX_MESSAGE:
        raise WireError('message too long')
    data = _object(text)
    match data.get('kind'):
        case 'mouse_down':
            return MouseDown(at=_point(data), button=_button(data))
        case 'mouse_move':
            return MouseMove(at=_point(data))
        case 'mouse_up':
            return MouseUp(at=_point(data), button=_button(data))
        case 'click':
            return Click(target=_point(data))
        case 'type':
            return Type(text=_str(data, 'text'))
        case 'press':
            return Press(key=_str(data, 'key'), modifiers=_modifiers(data))
        case 'scroll':
            at = _point(data) if 'x' in data or 'y' in data else None
            return Scroll(delta_x=_number(data, 'delta_x', 0), delta_y=_number(data, 'delta_y', 0), at=at)
        case 'switch_tab':
            return SwitchTab(tab_id=_str(data, 'tab_id'))
        case 'viewport':
            return ViewportSize(
                width=round(_bounded(data, 'width', _SIZES)),
                height=round(_bounded(data, 'height', _SIZES)),
            )
        case 'give_back':
            return GiveBackRequest()
        case _:
            raise WireError('unknown kind')


def encode_client(message: ClientMessage) -> str:
    data: dict[str, object]
    match message:
        case MouseDown(at=at, button=button) | MouseUp(at=at, button=button):
            data = {'kind': message.kind, 'x': at.x, 'y': at.y, 'button': button}
        case MouseMove(at=at):
            data = {'kind': message.kind, 'x': at.x, 'y': at.y}
        case Click(target=Point() as at):
            data = {'kind': message.kind, 'x': at.x, 'y': at.y}
        case Click():
            raise WireError('the live view clicks at points only')
        case Type(text=text, target=None):
            data = {'kind': message.kind, 'text': text}
        case Type():
            raise WireError('the live view types at the caret only')
        case Press(key=key, modifiers=modifiers):
            data = {'kind': message.kind, 'key': key, 'modifiers': list(modifiers)}
        case Scroll(delta_x=delta_x, delta_y=delta_y, at=at):
            data = {'kind': message.kind, 'delta_x': delta_x, 'delta_y': delta_y}
            if at is not None:
                data |= {'x': at.x, 'y': at.y}
        case SwitchTab(tab_id=tab_id):
            data = {'kind': message.kind, 'tab_id': tab_id}
        case ViewportSize(width=width, height=height):
            data = {'kind': message.kind, 'width': width, 'height': height}
        case GiveBackRequest():
            data = {'kind': message.kind}
    return json.dumps(data)


# --- the server's messages ---


def encode_server(message: ServerMessage) -> str:
    data: dict[str, object]
    match message:
        case Hello(handoff_id=handoff_id, reason=reason):
            data = {'kind': message.kind, 'handoff_id': handoff_id, 'reason': reason}
        case Tabs(tabs=tabs):
            data = {
                'kind': 'tabs',
                'tabs': [{'tab_id': t.tab_id, 'url': t.url, 'title': t.title, 'active': t.active} for t in tabs],
            }
        case ErrorMessage(message=text):
            data = {'kind': message.kind, 'message': text}
        case Ended(given_back=given_back):
            data = {'kind': message.kind, 'given_back': given_back}
    return json.dumps(data)


def decode_server(text: str) -> ServerMessage:
    data = _object(text)
    match data.get('kind'):
        case 'hello':
            return Hello(handoff_id=_str(data, 'handoff_id'), reason=_str(data, 'reason'))
        case 'tabs':
            tabs = data.get('tabs')
            if not isinstance(tabs, list):
                raise WireError('tabs must be a list')
            return Tabs(tabs=tuple(_tab(t) for t in cast(list[object], tabs)))
        case 'error':
            return ErrorMessage(message=_str(data, 'message'))
        case 'ended':
            return Ended(given_back=data.get('given_back') is True)
        case _:
            raise WireError('unknown kind')


def encode_frame(frame: Frame, seq: int) -> bytes:
    header = json.dumps({'seq': seq, 'width': frame.width, 'height': frame.height, 'mime': frame.mime}).encode()
    return struct.pack('>I', len(header)) + header + frame.image


def decode_frame(data: bytes) -> NumberedFrame:
    if len(data) < 4:
        raise WireError('frame too short')
    (length,) = struct.unpack('>I', data[:4])
    header = _object(data[4 : 4 + length].decode())
    mime = header.get('mime')
    if mime not in _MIMES:
        raise WireError('unknown image type')
    frame = Frame(
        image=data[4 + length :],
        mime=mime,
        width=int(_number(header, 'width')),
        height=int(_number(header, 'height')),
    )
    return NumberedFrame(seq=int(_number(header, 'seq')), frame=frame)


# --- checking JSON ---


def _object(text: str) -> dict[str, object]:
    try:
        data: object = json.loads(text)
    except ValueError as error:
        raise WireError('not JSON') from error
    if not isinstance(data, dict):
        raise WireError('not a JSON object')
    return cast(dict[str, object], data)


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise WireError(f'{key} must be a string')
    return value


def _number(data: dict[str, object], key: str, default: float | None = None) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise WireError(f'{key} must be a number')
    return float(value)


def _bounded(data: dict[str, object], key: str, bounds: tuple[float, float]) -> float:
    value = _number(data, key)
    if not bounds[0] <= value <= bounds[1]:
        raise WireError(f'{key} must be from {bounds[0]} to {bounds[1]}')
    return value


def _point(data: dict[str, object]) -> Point:
    return Point(x=_number(data, 'x'), y=_number(data, 'y'))


def _button(data: dict[str, object]) -> MouseButton:
    button = data.get('button', 'left')
    if button not in get_args(MouseButton):
        raise WireError('unknown button')
    return cast(MouseButton, button)


def _modifiers(data: dict[str, object]) -> tuple[Modifier, ...]:
    modifiers = data.get('modifiers', [])
    if not isinstance(modifiers, list):
        raise WireError('modifiers must be a list')
    items = cast(list[object], modifiers)
    if any(m not in get_args(Modifier) for m in items):
        raise WireError('unknown modifier')
    return tuple(cast(list[Modifier], items))


def _tab(data: object) -> Tab:
    if not isinstance(data, dict):
        raise WireError('a tab must be an object')
    tab = cast(dict[str, object], data)
    return Tab(
        tab_id=_str(tab, 'tab_id'), url=_str(tab, 'url'), title=_str(tab, 'title'), active=tab.get('active') is True
    )
