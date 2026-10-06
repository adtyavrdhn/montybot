"""The user paths that need only a browser: U1 read the web and U6 a bot check the user solves. U2 and U3 are in
`test_sign_ins.py` and `test_skeleton.py`; U4 (schedules) comes with #9, and U5 (files) is in
`test_files.py`."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from conftest import Client, Human
from sites.checkpoint import HOLD_MS, Checkpoint
from sites.flights import Flights, cheapest


@pytest.fixture
def flights() -> Iterator[Flights]:
    site = Flights()
    site.start()
    yield site
    site.stop()


@pytest.fixture
def checkpoint() -> Iterator[Checkpoint]:
    site = Checkpoint()
    site.start()
    yield site
    site.stop()


@pytest.mark.u1
def test_read_the_web(client: Client, flights: Flights) -> None:
    client.sign_up()
    thread = client.ask(f'Find the three cheapest flights to Lisbon next Friday on {flights.url} and send me a table.')
    reply = client.wait_for_reply(thread)
    named = re.findall(r'[A-Z0-9]{2} \d{3,4}', reply)
    assert named[:3] == [flight for flight, _, _ in cheapest(3)]  # the cheapest is on the second page


@pytest.mark.u6
def test_the_user_passes_a_press_and_hold_check(client: Client, checkpoint: Checkpoint) -> None:
    client.sign_up()
    thread = client.ask(f'What is on offer today at {checkpoint.url}?')
    handoff = client.wait_for_ask(thread, 'handoff')
    assert 'hold' in handoff['prompt'].lower()

    human = Human(client, client.thread(thread)['run']['id'])
    human.press_and_hold(seconds=HOLD_MS / 1000 + 0.5)

    assert 'oat milk' in client.wait_for_reply(thread).lower()
    assert checkpoint.passes == 1
