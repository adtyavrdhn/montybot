"""Frame rate and input latency of the live view, end to end on localhost: engine, frame source, app, WebSocket,
client. Opt in with `SAMMY_MEASURE=1 uv run pytest tests/liveview/test_liveview_measure.py -s`.

- Frame rate: frames the client receives per second while `/animate` changes every animation frame.
- Input latency: from sending `mouse_down` (or `mouse_up`) to receiving the first frame where `/bench` has turned
  black (or white again). It includes the engine painting and encoding, so it is what the user waits for.
"""

from __future__ import annotations

import asyncio
import io
import os
import statistics
import time

import pytest
from liveview_harness import StubBrowserService, backends, serve_app, serve_fixtures
from PIL import Image

from sammy.browser.contract import MouseDown, MouseUp, Navigate, Point
from sammy.liveview.app import live_view_app
from sammy.liveview.auth import StubAuthenticator
from sammy.liveview.client import LiveViewClient, ReceivedFrame
from sammy.liveview.handoffs import InMemoryHandoffs

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get('SAMMY_MEASURE') != '1', reason='set SAMMY_MEASURE=1 to measure'),
]

SECONDS = 5
PRESSES = 20


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def centre_is_dark(received: ReceivedFrame) -> bool:
    image = Image.open(io.BytesIO(received.frame.image)).convert('L')
    return image.getpixel((image.width // 2, image.height // 2)) < 128  # type: ignore[operator]


@pytest.mark.parametrize('engine', ['chromium', 'servo'])
async def test_measure(engine: str) -> None:
    with serve_fixtures() as origin:
        async with backends(engine) as new:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            try:
                await service.start(run_id='run', user_id='alice')
                await service.act(run_id='run', user_id='alice', action=Navigate(url=f'{origin}/animate'))
                handoff = await service.start_handoff(run_id='run', user_id='alice', reason='measure')
                handoffs.add(handoff)
                ids = {'run_id': 'run', 'user_id': 'alice', 'handoff_id': handoff.handoff_id}
                app = live_view_app(service=service, handoffs=handoffs, auth=auth)
                async with serve_app(app) as base:
                    ws = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
                    async with LiveViewClient.connect(ws, session=auth.sign_in('alice')) as user:
                        await user.next_frame(after=0)
                        await asyncio.sleep(1)
                        start_frames, started = user.frames, time.monotonic()
                        sizes: list[int] = []
                        while time.monotonic() - started < SECONDS:
                            sizes.append(len((await user.next_frame()).frame.image))
                        fps = (user.frames - start_frames) / (time.monotonic() - started)
                        assert user.frame is not None
                        width, height = user.frame.frame.width, user.frame.frame.height

                    # Latency on a still page that turns black while the button is down.
                    await service.act(**ids, action=Navigate(url=f'{origin}/bench'))
                    async with LiveViewClient.connect(ws, session=auth.sign_in('alice')) as user:
                        latest = await user.next_frame(after=0)
                        while centre_is_dark(latest):
                            latest = await user.next_frame()
                        await asyncio.sleep(0.5)
                        latencies: list[float] = []
                        at = Point(x=200, y=200)
                        for press in range(PRESSES * 2):
                            down = press % 2 == 0
                            seq = user.frame.seq if user.frame else 0
                            sent = time.monotonic()
                            await user.send(MouseDown(at=at) if down else MouseUp(at=at))
                            while True:
                                received = await user.next_frame(after=seq)
                                seq = received.seq
                                if centre_is_dark(received) == down:
                                    break
                            latencies.append((received.received_at - sent) * 1000)
                            await asyncio.sleep(0.1)
                        idle_from = user.frames
                        await asyncio.sleep(2)
                        idle_fps = (user.frames - idle_from) / 2
            finally:
                await service.close_all()
    latencies.sort()
    print(
        f'\n[measure] {engine}, {width}x{height}: {fps:.1f} frames/s on an animated page '
        f'(median frame {statistics.median(sizes) / 1024:.0f} KiB); {idle_fps:.1f} frames/s on a still page; '
        f'input to frame: median {statistics.median(latencies):.0f} ms, '
        f'p90 {latencies[int(len(latencies) * 0.9) - 1]:.0f} ms, max {latencies[-1]:.0f} ms '
        f'over {len(latencies)} mouse downs and ups'
    )
