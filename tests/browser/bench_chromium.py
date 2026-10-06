"""Measure `ChromiumBackend` per browser: start time, page load, resident memory and CPU. Not a test; run it by hand:

    uv run python tests/browser/bench_chromium.py [--runs 5] [--server]

For each mode (headed, headless) and page (the poc demo shop signed in, example.com), it starts a browser, loads the
page, waits for it to settle, and sums over every process started for that browser (Chrome's, and on the server
bwrap's and Xvfb's): resident memory (RSS, so shared pages count once per process) and CPU time. It prints the median
of `--runs`. `--server` uses `ChromiumOptions.server()` for the headed run (Linux only).
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import importlib.util
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.contract import Navigate
from montybot.browser.state import BrowserState, Cookie

ROOT = Path(__file__).resolve().parents[2]
SETTLE = 2.0
IDLE = 5.0


@dataclass(frozen=True, kw_only=True)
class Usage:
    processes: int
    rss_mib: float
    cpu_seconds: float


@dataclass(frozen=True, kw_only=True)
class Run:
    start: float
    load: float
    close: float
    usage: Usage
    cpu_start_and_load: float
    idle_cpu_percent: float


def _process_table() -> list[tuple[int, int, int, float, str]]:
    """(pid, ppid, rss bytes, cpu seconds, command line) for every process."""
    if sys.platform == 'linux':
        rows: list[tuple[int, int, int, float, str]] = []
        tick, page = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except OSError:
                continue
            ppid, cpu, rss = int(stat[1]), (int(stat[11]) + int(stat[12])) / tick, int(stat[21]) * page
            rows.append((int(entry.name), ppid, rss, cpu, command))
        return rows
    ps = ['ps', '-A', '-o', 'pid=,ppid=,rss=,time=,command=']
    rows = []
    for line in subprocess.run(ps, capture_output=True, text=True, check=True).stdout.splitlines():
        pid, ppid, rss, cpu, command = line.split(None, 4)
        minutes, seconds = cpu.rsplit(':', 1)
        hours = 0
        if ':' in minutes:
            hours, minutes = minutes.split(':')
        rows.append(
            (int(pid), int(ppid), int(rss) * 1024, int(hours) * 3600 + int(minutes) * 60 + float(seconds), command)
        )
    return rows


def usage(workdir: Path) -> Usage:
    """Every process with `workdir` on its command line, and their descendants."""
    table = _process_table()
    chosen = {pid for pid, _, _, _, command in table if str(workdir) in command}
    while True:
        more = {pid for pid, ppid, _, _, _ in table if ppid in chosen} - chosen
        if not more:
            break
        chosen |= more
    rows = [row for row in table if row[0] in chosen]
    return Usage(processes=len(rows), rss_mib=sum(r[2] for r in rows) / 2**20, cpu_seconds=sum(r[3] for r in rows))


def start_demo_shop() -> tuple[str, Cookie]:
    """Serve `poc/`'s demo shop on a free port, sign in, and return its origin and the HttpOnly login cookie."""
    spec = importlib.util.spec_from_file_location('demo_site', ROOT / 'poc/montybot_poc/demo_site.py')
    assert spec and spec.loader
    demo_site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_site)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = demo_site.start(port)
    connection = http.client.HTTPConnection('127.0.0.1', port)
    connection.request(
        'POST', '/login', 'username=bench&password=hunter2', {'Content-Type': 'application/x-www-form-urlencoded'}
    )
    sid = connection.getresponse().getheader('Set-Cookie', '').split(';')[0].split('=', 1)[1]
    return origin, Cookie(name='sid', value=sid, domain='127.0.0.1', http_only=True)


async def measure(backend: ChromiumBackend, url: str, cookies: list[Cookie], expect: str) -> Run:
    try:
        started = time.perf_counter()
        await backend.open(BrowserState(cookies=cookies))  # seeds cookies, loads nothing
        start = time.perf_counter() - started
        started = time.perf_counter()
        await backend.act(Navigate(url=url))
        load = time.perf_counter() - started
        snapshot = await backend.snapshot()
        assert expect in f'{snapshot.title}\n{snapshot.text}', f'{url} did not show {expect!r}'
        assert backend.workdir is not None
        await asyncio.sleep(SETTLE)
        settled = usage(backend.workdir)
        await asyncio.sleep(IDLE)
        idle = usage(backend.workdir)
        started = time.perf_counter()
    finally:
        await backend.close()
    return Run(
        start=start,
        load=load,
        close=time.perf_counter() - started,
        usage=settled,
        cpu_start_and_load=settled.cpu_seconds,
        idle_cpu_percent=100 * (idle.cpu_seconds - settled.cpu_seconds) / IDLE,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--server', action='store_true', help='headed runs use ChromiumOptions.server() (Linux)')
    args = parser.parse_args()

    shop, sid = start_demo_shop()
    pages = {
        'demo shop': (f'{shop}/shop', [sid], 'Signed in as bench'),
        'example.com': ('https://example.com/', [], 'Example Domain'),
    }
    print(
        f'{args.runs} runs each, medians. CPU = CPU time over start + load + {SETTLE:g} s; idle = the next {IDLE:g} s'
    )
    print('| Mode | Page | Start s | Load s | Processes | RSS MiB | CPU s | Idle CPU % | Close s |')
    print('|---|---|---|---|---|---|---|---|---|')
    async with async_playwright() as playwright:
        modes = {
            'headed': ChromiumOptions.server(allow_private_networks=True) if args.server else ChromiumOptions(),
            'headless (shell)': ChromiumOptions(headless=True),
            # The same Chrome as headed, in its new headless mode: what bwrap runs with `server(headless=True)`.
            'headless (full Chrome)': ChromiumOptions(
                headless=True, executable_path=playwright.chromium.executable_path
            ),
        }
        for mode, options in modes.items():
            for page, (url, cookies, expect) in pages.items():
                runs = [
                    await measure(ChromiumBackend(playwright=playwright, options=options), url, cookies, expect)
                    for _ in range(args.runs)
                ]

                def median(values: list[float]) -> float:
                    return statistics.median(values)

                print(
                    f'| {mode} | {page} | {median([r.start for r in runs]):.2f} | {median([r.load for r in runs]):.2f}'
                    f' | {median([r.usage.processes for r in runs]):.0f} | {median([r.usage.rss_mib for r in runs]):.0f}'
                    f' | {median([r.cpu_start_and_load for r in runs]):.2f}'
                    f' | {median([r.idle_cpu_percent for r in runs]):.1f} | {median([r.close for r in runs]):.2f} |'
                )


if __name__ == '__main__':
    asyncio.run(main())
