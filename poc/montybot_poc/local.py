"""The user's side: wait for a hand-off, open a sandboxed browser window seeded with the agent's session,
and send the session back when the user clicks "Return control to agent".

The window is "sandboxed" in that it is a separate Chromium with a throwaway profile (never your own
browser profile), Chromium's sandbox switched on, and downloads off. Closing the window or quitting the
browser also returns control; after a quit, the state comes from the last page load.

Run: uv run python -m montybot_poc.local --token TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page, Playwright, async_playwright

from montybot_poc import demo_site
from montybot_poc.browser import export_state, open_page, seeded_context
from montybot_poc.state import BrowserState
from montybot_poc.wire import DEFAULT_PORT, LINE_LIMIT, recv, send

_RETURN_BAR = """
window.addEventListener('DOMContentLoaded', () => {
  if (window.top !== window) return;
  const bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;display:flex;gap:12px;'
    + 'align-items:center;justify-content:center;padding:10px;background:#111827;color:#f9fafb;font:14px system-ui';
  const label = document.createElement('span');
  label.textContent = 'montybot needs you: ' + %s;
  const button = document.createElement('button');
  button.id = 'montybot-return';
  button.textContent = 'Return control to agent';
  button.style.cssText = 'padding:6px 12px;border:0;border-radius:6px;background:#34d399;font-weight:600;cursor:pointer';
  button.onclick = () => window.montybotReturn();
  bar.append(label, button);
  document.documentElement.append(bar);
});
"""


async def take_over(
    playwright: Playwright, state: BrowserState, reason: str, *, auto_demo: bool
) -> tuple[BrowserState, str]:
    """Open a window on `state`, wait for the user to return control, and export what they left."""
    browser = await playwright.chromium.launch(headless=auto_demo, chromium_sandbox=True)
    done: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def finish(note: str) -> None:
        if not done.done():
            done.set_result(note)

    try:
        context = await seeded_context(browser, state, accept_downloads=False, no_viewport=not auto_demo)
        await context.expose_function('montybotReturn', lambda: finish('user clicked Return control'))
        await context.add_init_script(script=_RETURN_BAR % json.dumps(reason))

        last_page: Page | None = None
        last_url = state.url
        snapshot = state  # the state as of the last page load, in case the user quits the browser

        async def remember(page: Page) -> None:
            nonlocal snapshot
            try:
                snapshot = await export_state(context, page)
            except PlaywrightError:
                pass

        def on_navigate(frame: Frame) -> None:
            nonlocal last_url
            if frame.parent_frame is None and frame.url.startswith('http'):
                last_url = frame.url

        def on_close(_: Page) -> None:
            if all(p.is_closed() for p in context.pages):
                finish('user closed the window')

        def track(page: Page) -> None:
            nonlocal last_page
            last_page = page
            page.on('framenavigated', on_navigate)
            page.on('load', remember)
            page.on('close', on_close)

        context.on('page', track)
        browser.on('disconnected', lambda _: finish('browser quit'))
        page = await open_page(context, state)
        if last_page is None:
            track(page)

        if auto_demo:
            await _simulate_user(page)
        note = await done

        try:
            returned = await export_state(context, last_page)
        except PlaywrightError:
            return snapshot, f'{note}; returned the state as of the last page load'
        if not returned.url.startswith('http'):
            returned.url = last_url
        return returned, note
    finally:
        try:
            await browser.close()
        except PlaywrightError:
            pass


async def _simulate_user(page: Page) -> None:
    """--auto-demo: sign in to the demo shop, add one extra item, and click Return."""
    parts = urlsplit(page.url)
    await page.goto(f'{parts.scheme}://{parts.netloc}/login')
    await page.fill('#username', 'mike')
    await page.fill('#password', demo_site.PASSWORD)
    await page.click('button[type=submit]')
    await page.wait_for_url('**/shop')
    await page.click('#add-coffee')
    await page.click('#montybot-return')


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--token', required=True)
    parser.add_argument('--auto-demo', action='store_true', help='play the user headlessly (for tests)')
    args = parser.parse_args()

    reader, writer = await asyncio.open_connection(args.host, args.port, limit=LINE_LIMIT)
    await send(writer, {'type': 'hello', 'token': args.token})
    print('[local] connected; waiting for the agent to ask for help')
    async with async_playwright() as playwright:
        while True:
            try:
                message = await recv(reader)
            except ConnectionError:
                print('[local] the remote side closed the connection')
                return
            state = BrowserState.from_json(message['state'])
            print(f'[local] agent needs help: {message["reason"]}')
            print(f'[local]   received: {state.summary()}')
            returned, note = await take_over(playwright, state, message['reason'], auto_demo=args.auto_demo)
            print(f'[local]   returning ({note}): {returned.summary()}')
            await send(writer, {'type': 'returned', 'note': note, 'state': returned.to_json()})


if __name__ == '__main__':
    asyncio.run(main())
