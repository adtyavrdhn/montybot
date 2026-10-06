"""U5 files (#21): what the browser downloads lands in the user's files, where the agent's code reads it, and another
user's code cannot. With the CPython tier (#6), pandas reads the same files; that test needs Linux with bwrap
(`tests/linux/run.sh tests/e2e/test_files.py` runs it in a Linux container)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import App, Client
from sites.invoices import INVOICES, Invoices, csv_of, last_three_total

from montybot.cpython import can_jail

NEEDS_LINUX = 'the CPython tier runs in bwrap, which needs Linux: run tests/linux/run.sh'


@pytest.fixture
def invoices() -> Iterator[Invoices]:
    site = Invoices()
    site.start()
    yield site
    site.stop()


@pytest.mark.u5
@pytest.mark.scripted
def test_download_invoices_and_total_them(app: App, client: Client, invoices: Invoices, workspaces_dir: Path) -> None:
    alice = client.sign_up()
    thread = client.ask(f'Download my last three invoices from {invoices.url} and total them.')
    assert f'€{last_three_total():.2f}' in client.wait_for_reply(thread)
    assert invoices.downloads == 3

    # The files are in Alice's folder on the server, as the site sent them.
    downloads = workspaces_dir / alice['id'] / 'downloads'
    assert sorted(p.name for p in downloads.iterdir()) == [f'invoice-{month}.csv' for month, _ in INVOICES[-3:]]
    month, lines = INVOICES[-1]
    assert (downloads / f'invoice-{month}.csv').read_text() == csv_of(lines)

    # Bob's code sees an empty folder, and cannot reach Alice's files by path.
    bob = Client(app)
    try:
        bob.sign_up()
        thread = bob.ask(f'Show me my files and the file /work/../{alice["id"]}/downloads/invoice-{month}.csv')
        reply = bob.wait_for_reply(thread)
    finally:
        bob.http.close()
    assert reply.startswith('[]') and 'PermissionError' in reply, reply


@pytest.mark.u5
@pytest.mark.scripted
@pytest.mark.skipif(not can_jail(), reason=NEEDS_LINUX)
def test_total_invoices_with_pandas(client: Client, invoices: Invoices, workspaces_dir: Path) -> None:
    """#6: the browser's downloads, Monty's pathlib and pandas in the CPython jail all see the same files."""
    alice = client.sign_up()
    thread = client.ask(f'Total my last three invoices with pandas from {invoices.url}.')
    reply = client.wait_for_reply(thread)
    total = f'{last_three_total():.2f}'
    assert reply == f'Your last three invoices come to €{total} (Monty got €{total}; read back: {total}).'
    files = workspaces_dir / alice['id']
    assert (files / 'pandas-total.txt').read_text() == (files / 'monty-total.txt').read_text() == total
