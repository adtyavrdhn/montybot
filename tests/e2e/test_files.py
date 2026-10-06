"""U5 files (#21): what the browser downloads lands in the user's files, where the agent's code reads it, and another
user's code cannot."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import App, Client
from sites.invoices import INVOICES, Invoices, csv_of, last_three_total


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
