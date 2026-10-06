from __future__ import annotations

import time

import pytest

from montybot.browser.contract import Click, MouseDown, MouseMove, MouseUp, Point, Press, Scroll, Selector, Type
from montybot.browser.live import Frame, Tab, Tabs
from montybot.liveview.activity import Activity
from montybot.liveview.keys import key_for
from montybot.liveview.wire import (
    ClientMessage,
    Ended,
    ErrorMessage,
    GiveBackRequest,
    Hello,
    ServerMessage,
    SwitchTab,
    WireError,
    decode_client,
    decode_frame,
    decode_server,
    encode_client,
    encode_frame,
    encode_server,
)

AT = Point(x=10.5, y=20)


@pytest.mark.parametrize(
    'message',
    [
        MouseDown(at=AT),
        MouseDown(at=AT, button='right'),
        MouseMove(at=AT),
        MouseUp(at=AT, button='middle'),
        Click(target=AT),
        Type(text='hello wörld'),
        Press(key='Enter'),
        Press(key='b', modifiers=('Control', 'Shift')),
        Scroll(delta_y=120),
        Scroll(delta_x=-5, delta_y=0, at=AT),
        SwitchTab(tab_id='2'),
        GiveBackRequest(),
    ],
)
def test_client_messages_round_trip(message: ClientMessage) -> None:
    assert decode_client(encode_client(message)) == message


@pytest.mark.parametrize(
    'text',
    [
        'not json',
        '[]',
        '{"kind": "navigate", "url": "http://a.test/"}',
        '{"kind": "mouse_down", "x": "1", "y": 2}',
        '{"kind": "mouse_down", "x": NaN, "y": 2}',
        '{"kind": "mouse_down", "x": true, "y": 2}',
        '{"kind": "mouse_up", "x": 1, "y": 2, "button": "fourth"}',
        '{"kind": "press", "key": "a", "modifiers": ["Hyper"]}',
        '{"kind": "type"}',
        '{"kind": "switch_tab", "tab_id": 3}',
        '{"kind": "type", "text": "' + 'x' * 70_000 + '"}',
    ],
)
def test_bad_client_messages_are_refused(text: str) -> None:
    with pytest.raises(WireError):
        decode_client(text)


def test_the_live_view_only_clicks_points_and_types_at_the_caret() -> None:
    with pytest.raises(WireError):
        encode_client(Click(target=Selector(css='#a')))
    with pytest.raises(WireError):
        encode_client(Type(text='a', target=Selector(css='#a')))


@pytest.mark.parametrize(
    'message',
    [
        Hello(handoff_id='h1', reason='Please sign in'),
        Tabs(tabs=(Tab(tab_id='1', url='http://a.test/', title='A', active=True),)),
        ErrorMessage(message='servo does not support press'),
        Ended(given_back=True),
    ],
)
def test_server_messages_round_trip(message: ServerMessage) -> None:
    assert decode_server(encode_server(message)) == message


def test_frames_round_trip() -> None:
    frame = Frame(image=b'\xff\xd8jpeg bytes', mime='image/jpeg', width=1280, height=720)
    numbered = decode_frame(encode_frame(frame, 7))
    assert (numbered.seq, numbered.frame) == (7, frame)


def test_keys() -> None:
    enter = key_for('Enter')
    assert enter is not None and (enter.code, enter.key_code, enter.text, enter.webdriver) == (
        'Enter',
        13,
        '\r',
        '\ue007',
    )
    a = key_for('a')
    assert a is not None and (a.code, a.key_code, a.text, a.webdriver) == ('KeyA', 65, 'a', 'a')
    f5 = key_for('F5')
    assert f5 is not None and (f5.key_code, f5.webdriver) == (116, '\ue035')
    assert key_for('\n') == enter
    assert key_for('NoSuchKey') is None


def test_the_summary_counts_but_never_says_what_was_typed() -> None:
    activity = Activity(started=time.monotonic() - 65)
    activity.saw(Tabs(tabs=(Tab(tab_id='1', url='', title='', active=True),)))
    for action in (
        MouseDown(at=AT),
        MouseMove(at=AT),
        MouseUp(at=AT),
        Click(target=AT),
        Type(text='hunter2'),
        Press(key='Enter'),
        Scroll(delta_y=5),
    ):
        activity.record(action)
    activity.saw(
        Tabs(
            tabs=(
                Tab(tab_id='1', url='', title='', active=False),
                Tab(tab_id='2', url='', title='', active=True),
            )
        )
    )
    summary = activity.summary('http://shop.test/account')
    assert summary == (
        'The user had the browser for 1 min 5 s. They clicked 2 times, pressed 1 key, typed 7 characters, scrolled '
        'once and opened 1 tab. They left it on http://shop.test/account.'
    )
    assert 'hunter2' not in summary
    assert Activity().summary('about:blank') == (
        'The user had the browser for 0 s. They did not click or type anything. They left it on about:blank.'
    )
