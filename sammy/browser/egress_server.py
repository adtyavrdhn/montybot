"""Run the public-only SOCKS proxy without the app's database network or credentials."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from sammy.browser.egress import EgressProxy


async def serve(path: Path) -> None:
    proxy = EgressProxy(path)
    await proxy.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    try:
        await stopped.wait()
    finally:
        await proxy.stop()


if __name__ == '__main__':
    asyncio.run(serve(Path(sys.argv[1])))
