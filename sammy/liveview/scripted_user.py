"""The scripted user: drives a hand-off over its WebSocket as a person would through the page, for tests and the
end-to-end harness (#2, #17). Like `poc/sammy_poc/local.py --auto-demo`, but through the live view.

The user only sees pixels, and where things land on the page depends on the engine's fonts, so the shop is driven
with the keyboard (Tab, typing, Enter), and the hold check with the mouse at a point the caller knows.

Run against a live hand-off:

    uv run python -m sammy.liveview.scripted_user ws://127.0.0.1:PORT/handoff/ID/ws --session TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from urllib.parse import urlsplit

from sammy.browser.contract import MouseDown, MouseMove, MouseUp, Point, Press, Type
from sammy.liveview.client import LiveViewClient

DEMO_SHOP_PASSWORD = 'hunter2'
"""The password `poc/sammy_poc/demo_site.py` accepts."""
SETTLE = 0.3
"""Seconds to wait after a new page shows before typing into it, as a person would."""


async def sign_in_to_demo_shop(
    client: LiveViewClient, *, username: str = 'mike', password: str = DEMO_SHOP_PASSWORD, add: str | None = 'coffee'
) -> None:
    """From the poc demo shop's sign-in wall: open the sign-in page, sign in, and add `add` to the cart.

    Returns on the shop page. The shop's buttons are, in order, eggs, milk, bread and coffee.
    """
    await client.wait_for_url(lambda url: urlsplit(url).path in ('/', '/shop'))
    await _shown(client)
    await client.send(Press(key='Tab'))  # the "Sign in" link
    await client.send(Press(key='Enter'))
    await client.wait_for_url(lambda url: urlsplit(url).path == '/login')
    await _shown(client)
    await client.send(Press(key='Tab'))
    await client.send(Type(text=username))
    await client.send(Press(key='Tab'))
    await client.send(Type(text=password))
    # Tab to the button rather than Enter in the field: Servo 0.7.0 does not submit a form on Enter in a text field.
    await client.send(Press(key='Tab'))
    await client.send(Press(key='Enter'))
    await client.wait_for_url(lambda url: urlsplit(url).path == '/shop')
    if add is not None:
        await _shown(client)
        for _ in range(['eggs', 'milk', 'bread', 'coffee'].index(add) + 1):
            await client.send(Press(key='Tab'))
        await client.send(Press(key='Enter'))
        await asyncio.sleep(SETTLE)


async def press_and_hold(
    client: LiveViewClient, *, at: Point, seconds: float = 2, done: Callable[[str], bool] | None = None
) -> None:
    """Hold the left button at `at` for `seconds`, moving a little as a hand does, then let go.

    With `done`, wait until the active tab's URL meets it.
    """
    await _shown(client)
    await client.send(MouseDown(at=at))
    steps = 8
    for step in range(steps):
        await asyncio.sleep(seconds / steps)
        await client.send(MouseMove(at=Point(x=at.x + (step % 3) - 1, y=at.y + (step % 2))))
    await client.send(MouseUp(at=at))
    if done is not None:
        await client.wait_for_url(done)


async def _shown(client: LiveViewClient) -> None:
    """Wait until a frame of the current page has arrived, then a moment more."""
    await client.wait_until(lambda: client.frame is not None)
    await asyncio.sleep(SETTLE)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('url', help='the hand-off WebSocket, ws://HOST/handoff/ID/ws')
    parser.add_argument('--session', required=True, help='the sammy_session cookie of the run requester')
    parser.add_argument('--username', default='mike')
    args = parser.parse_args()
    async with LiveViewClient.connect(args.url, session=args.session) as client:
        await client.wait_until(lambda: client.hello is not None)
        assert client.hello is not None
        print(f'[user] the bot asks: {client.hello.reason}')
        await sign_in_to_demo_shop(client, username=args.username)
        print('[user] signed in and added coffee; giving the browser back')
        await client.give_back()


if __name__ == '__main__':
    asyncio.run(main())
