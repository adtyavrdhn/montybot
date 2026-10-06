"""One control load of walmart search with Chromium headless shell (same launch as ../engines), same egress and hour
as the Servo runs, so the Servo walmart result can be compared like for like.

usage: PLAYWRIGHT_BROWSERS_PATH=../engines/bin/ms-playwright uv run --no-project --with playwright==1.63.0 \
         python scripts/chromium_control.py
"""

import json
import pathlib
import time

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).resolve().parents[1] / 'out'
URL = 'https://www.walmart.com/search?q=milk'

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={'width': 1280, 'height': 800})
    t = time.perf_counter()
    page.goto(URL, wait_until='load', timeout=60000)
    load = round(time.perf_counter() - t, 3)
    time.sleep(2)
    html = page.content()
    title = page.title()
    rec = {
        'engine': 'chromium-headless-shell', 'site': 'walmart-search', 'load_s': load, 'title': title, 'final_url': page.url,
        'challenge': [h for h, c in [('robot-or-human', 'robot or human' in (title + html).lower()),
                                     ('px-captcha', 'px-captcha' in html), ('/blocked', '/blocked' in page.url)] if c],
        'ua': page.evaluate('navigator.userAgent'),
    }
    page.screenshot(path=str(OUT / 'control-chromium-walmart-search.png'))
    b.close()
with open(OUT / 'results.jsonl', 'a') as f:
    f.write(json.dumps(rec) + '\n')
print(json.dumps(rec))
