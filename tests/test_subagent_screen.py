"""The live screen while subagents work (#132): their tabs side by side in one picture, as big as one tab."""

from __future__ import annotations

import io

from PIL import Image

from sammy.subagents import BACKGROUND, tile

RED = (220, 30, 30)
GREEN = (30, 200, 30)
BLUE = (30, 30, 220)


def png(color: tuple[int, int, int], size: tuple[int, int] = (1280, 720)) -> bytes:
    picture = io.BytesIO()
    Image.new('RGB', size, color).save(picture, format='PNG')
    return picture.getvalue()


def opened(picture: bytes) -> Image.Image:
    return Image.open(io.BytesIO(picture)).convert('RGB')


def test_one_tab_fills_the_screen() -> None:
    screen = opened(tile([png(RED)]))
    assert screen.size == (1280, 720)
    assert {screen.getpixel((0, 0)), screen.getpixel((640, 360)), screen.getpixel((1279, 719))} == {RED}


def test_three_tabs_take_two_rows_in_their_order() -> None:
    screen = opened(tile([png(RED), png(GREEN), png(BLUE)]))
    assert screen.size == (1280, 720)
    assert screen.getpixel((320, 180)) == RED
    assert screen.getpixel((960, 180)) == GREEN
    assert screen.getpixel((320, 540)) == BLUE
    assert screen.getpixel((960, 540)) == BACKGROUND  # no fourth tab
    assert screen.getpixel((640, 180)) == BACKGROUND  # the gap between two tabs


def test_two_tabs_sit_side_by_side_keeping_their_shape() -> None:
    screen = opened(tile([png(RED), png(GREEN)]))
    assert screen.getpixel((318, 360)) == RED
    assert screen.getpixel((960, 360)) == GREEN
    assert screen.getpixel((318, 40)) == BACKGROUND  # a 16:9 tab in a tall cell: bands above and below


def test_a_tab_not_seen_yet_leaves_its_place_empty() -> None:
    screen = opened(tile([None, png(GREEN, (800, 450))]))
    assert screen.size == (800, 450)  # the first frame there sets the size
    assert screen.getpixel((200, 225)) == BACKGROUND
    assert screen.getpixel((600, 225)) == GREEN
