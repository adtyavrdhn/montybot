# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright", "aiohttp"]
# ///
"""End-to-end check of takeover and state hand-off, with no real accounts.

1. A local site with a login form. Signing in sets an HttpOnly session cookie and a localStorage value.
2. takeover.run() opens it in headless Chromium. A scripted "human" connects to the takeover WebSocket, checks that
   frames arrive, clicks the fields, types the password, presses Enter and returns control.
3. A brand-new headless browser loads only the saved identity file and must land on the signed-in page.
"""

import asyncio
import json

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

import takeover

SITE_PORT, TAKEOVER_PORT = 8790, 8791
SITE = f'http://127.0.0.1:{SITE_PORT}'
LOGIN = """<!doctype html><title>Login</title><form method=post action=/login>
<input id=user name=user style="position:absolute;left:100px;top:100px;width:300px;height:40px">
<input id=pw name=pw type=password style="position:absolute;left:100px;top:200px;width:300px;height:40px">
<button style="position:absolute;left:100px;top:300px">Sign in</button></form>"""
HOME = """<!doctype html><title>Home</title><h1 id=who>signed in as {user}</h1>
<script>localStorage.setItem('prefs', 'dark'); document.title = 'Home ' + localStorage.getItem('prefs');</script>"""


async def site() -> web.AppRunner:
    async def index(request: web.Request) -> web.Response:
        user = request.cookies.get('session')
        if user:
            return web.Response(text=HOME.format(user=user), content_type='text/html')
        return web.Response(text=LOGIN, content_type='text/html')

    async def login(request: web.Request) -> web.Response:
        form = await request.post()
        if form.get('pw') != 'hunter2':
            raise web.HTTPFound('/')
        response = web.HTTPFound('/')
        response.set_cookie('session', str(form['user']), httponly=True)
        raise response

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_post('/login', login)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', SITE_PORT).start()
    return runner


async def human(link: str) -> int:
    ws_url = link.replace('http://', 'ws://').replace('/?', '/ws?')
    frames = 0
    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url) as ws:
        frames += await _drain(ws, 1)
        for x, y, text in [(250, 120, 'alice'), (250, 220, 'hunter2')]:
            await ws.send_json({'type': 'click', 'x': x, 'y': y})
            for ch in text:
                await ws.send_json({'type': 'key', 'key': ch})
        await ws.send_json({'type': 'key', 'key': 'Enter'})
        frames += await _drain(ws, 1)  # the signed-in page has been drawn
        await asyncio.sleep(0.5)
        await ws.send_json({'type': 'done'})
        async for _ in ws:
            pass
    return frames


async def _drain(ws: aiohttp.ClientWebSocketResponse, at_least: int) -> int:
    got = 0
    while got < at_least:
        msg = await asyncio.wait_for(ws.receive(), timeout=30)  # generous hang guard, not a timing assertion
        if msg.type == aiohttp.WSMsgType.TEXT:
            got += 1
    return got


async def main() -> None:
    runner = await site()
    takeover.IDENTITIES = takeover.IDENTITIES.parent / 'identities-selftest'
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(takeover.run(SITE, 'alice', TAKEOVER_PORT, ready))
    frames = await human(await ready)
    path = await task

    state = json.loads(path.read_text())
    cookie = next(c for c in state['cookies'] if c['name'] == 'session')
    assert cookie['httpOnly'] and cookie['value'] == 'alice', cookie
    assert any(o['origin'] == SITE and o['localStorage'] for o in state['origins']), state['origins']

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(path))
        page = await context.new_page()
        await page.goto(SITE)
        who = await page.text_content('#who')
        title = await page.title()
        await browser.close()
    await runner.cleanup()
    assert who == 'signed in as alice', who
    assert title == 'Home dark', title
    print(f'OK: {frames} frames streamed; HttpOnly cookie + localStorage saved; fresh browser is "{who}"')


if __name__ == '__main__':
    asyncio.run(main())
