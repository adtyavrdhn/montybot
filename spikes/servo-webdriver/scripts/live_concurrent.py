"""Live-view while acting: one thread polls Take Screenshot, another sends Actions on the same session.
Measures fps and action latency under contention (a takeover relay would do both at once)."""
import json
import statistics
import threading
import time

from wd import SPIKE, Servo

s = Servo(log=SPIKE / 'logs/live-concurrent.log')
d = s.session()
d.get('https://github.com/login')
time.sleep(1)
stop = threading.Event()
shots = []


def poll():
    while not stop.is_set():
        t = time.perf_counter()
        d.screenshot_png()
        shots.append(time.perf_counter() - t)


th = threading.Thread(target=poll)
t0 = time.perf_counter()
th.start()
el = d.find('#login_field')
d.click(el)
lat = []
for ch in 'takeover-typing-test':
    t = time.perf_counter()
    d.actions([{'type': 'key', 'id': 'kb', 'actions': [{'type': 'keyDown', 'value': ch}, {'type': 'keyUp', 'value': ch}]}])
    lat.append(time.perf_counter() - t)
for x in range(470, 800, 30):
    t = time.perf_counter()
    d.actions([{'type': 'pointer', 'id': 'm', 'parameters': {'pointerType': 'mouse'}, 'actions': [{'type': 'pointerMove', 'x': x, 'y': 300, 'duration': 0}]}])
    lat.append(time.perf_counter() - t)
stop.set()
th.join()
wall = time.perf_counter() - t0
r = {'wall_s': round(wall, 2), 'screenshots': len(shots), 'fps_while_acting': round(len(shots) / wall, 1),
     'shot_median_ms': round(statistics.median(shots) * 1000, 1), 'action_median_ms': round(statistics.median(lat) * 1000, 1),
     'action_p90_ms': round(sorted(lat)[int(len(lat) * .9)] * 1000, 1), 'typed_value': d.js("return document.querySelector('#login_field').value")}
d.quit()
s.close()
(SPIKE / 'out/checks-live-concurrent.json').write_text(json.dumps(r, indent=1))
print(json.dumps(r))
