"""Lightpanda-only follow-ups, since it allows one browser context per CDP connection.

- storage_state: export from one process, import into a fresh process (new_context(storage_state=...))
- two CDP connections to the same process: does each get its own isolated context?
- which CDP domains answer (Storage / Network cookies, DOM snapshot, Page.captureScreenshot, Input)

usage: PLAYWRIGHT_BROWSERS_PATH=... uv run --no-project --with playwright==1.63.0 --with psutil \
         python scripts/lightpanda_extra.py
"""

import json
import sys

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from measure import LOCAL, OUT, SITES, err, launch  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

res = {}
with sync_playwright() as p:
    # storage_state across processes
    b1, c1, _ = launch(p, 'lightpanda')
    try:
        ctx = b1.new_context()
        pg = ctx.new_page()
        pg.goto(f'{LOCAL}/set', wait_until='load')
        pg.evaluate("localStorage.setItem('spike', 'ls-value')")
        cdp = ctx.new_cdp_session(pg)
        for m in ('Storage.getCookies', 'Network.getAllCookies', 'Network.getCookies'):
            try:
                res[m] = cdp.send(m).get('cookies')
            except Exception as e:
                res[m] = {'error': err(e)}
        for m, params in (
            ('Page.captureScreenshot', {'format': 'png'}),
            ('DOMSnapshot.captureSnapshot', {'computedStyles': []}),
            ('Accessibility.getFullAXTree', {}),
            ('Input.insertText', {'text': 'x'}),
            ('Page.startScreencast', {'format': 'jpeg'}),
        ):
            try:
                r = cdp.send(m, params)
                res[m] = 'ok' + (f' ({len(json.dumps(r))} bytes)' if r else '')
            except Exception as e:
                res[m] = {'error': err(e)}
        try:
            state = ctx.storage_state()
            res['storage_state_export'] = state
        except Exception as e:
            state = None
            res['storage_state_export'] = {'error': err(e)}
    finally:
        b1.close()
        c1()

    if state:
        b2, c2, _ = launch(p, 'lightpanda')
        try:
            ctx2 = b2.new_context(storage_state=state)
            p2 = ctx2.new_page()
            p2.goto(f'{LOCAL}/check', wait_until='load')
            res['import_cookie_sent_to_server'] = 'sid=spike-httponly' in p2.content()
            p2.goto(SITES['login'], wait_until='load')
            res['import_localstorage'] = p2.evaluate("localStorage.getItem('spike')")
        except Exception as e:
            res['storage_state_import'] = {'error': err(e)}
        finally:
            b2.close()
            c2()

    # two CDP connections to one process, one context each
    b3, c3, notes = launch(p, 'lightpanda')
    try:
        ctxa = b3.new_context()
        pa = ctxa.new_page()
        pa.goto(f'{LOCAL}/set', wait_until='load')
        b4 = p.chromium.connect_over_cdp(f"http://127.0.0.1:{notes['port']}")
        ctxb = b4.new_context()
        pb = ctxb.new_page()
        pb.goto(f'{LOCAL}/check', wait_until='load')
        res['second_connection_body'] = pb.locator('body').inner_text()
        res['second_connection_isolated'] = 'sid=' not in res['second_connection_body']
        pa.goto(SITES['login'], wait_until='load')
        res['first_connection_still_works'] = pa.title()
        b4.close()
    except Exception as e:
        res['second_connection'] = {'error': err(e)}
    finally:
        b3.close()
        c3()

(OUT / 'lightpanda-extra.json').write_text(json.dumps(res, indent=1, default=str))
print(json.dumps(res, indent=1, default=str))
