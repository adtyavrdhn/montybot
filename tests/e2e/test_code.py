"""#5: the agent's code runs on Full Monty, and a run's Monty session is parked in monty-server's store while the run
waits, so no worker is held and the code finds its variables afterwards.

Needs a Full Monty pair: `MONTYBOT_TEST_MONTY_URL` (monty-server) and `MONTYBOT_TEST_MONTY_CONTAINERS` (the server and
worker containers, comma-separated, which the test restarts). `compose.yaml` runs both.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest
from conftest import Client, Human
from sites.shop import Shop

CONTAINERS = [c for c in os.environ.get('MONTYBOT_TEST_MONTY_CONTAINERS', '').split(',') if c]

pytestmark = pytest.mark.skipif(
    not (os.environ.get('MONTYBOT_TEST_MONTY_URL') and CONTAINERS),
    reason='needs Full Monty: MONTYBOT_TEST_MONTY_URL and MONTYBOT_TEST_MONTY_CONTAINERS',
)


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


@pytest.mark.u2
def test_monty_restarts_while_the_run_waits(client: Client, shop: Shop) -> None:
    client.sign_up()
    thread = client.ask(f'Order eggs from {shop.url}')  # the script's code sets `shop`, then the run hands off
    client.wait_for_ask(thread, 'handoff')

    # Nothing of the run lives in a Monty worker while it waits: restarting monty-server and monty-worker loses nothing.
    subprocess.run(['docker', 'restart', *CONTAINERS], check=True, capture_output=True)

    Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
    client.answer(client.wait_for_ask(thread, 'approval'), approved=True)
    assert '#1' in client.wait_for_reply(thread)  # the code after the hand-off used `shop` from before it
    assert [o.items for o in shop.orders] == [['eggs']]
