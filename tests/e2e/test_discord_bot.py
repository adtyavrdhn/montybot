"""#142: talking to Sammy on Discord, with the real app and a fake Discord (`tests/fake_discord.py`): a run in a direct
message, an approval with its button, the gateway connection dropped in the middle of the conversation, a server
mention answered in a thread, and two replicas of which only one connects. The shared flows are in
`test_channels.py`."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from conftest import App, Client
from fake_channel import Call
from fake_discord import BOT_ID, CHANNEL_ID, DM_ID, DiscordAPI, FakeGateway, click, message, new_id, payload
from helpers import eventually

from sammy.channels.inbound import BUSY

pytestmark = pytest.mark.scripted

WAIT = 180.0  # the CI machines and a loaded laptop are slow; tests wait for what the user would see


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    made = FakeGateway()
    yield made
    made.close()


@pytest.fixture
def api(gateway: FakeGateway) -> Iterator[DiscordAPI]:
    made = DiscordAPI(gateway)
    yield made
    made.close()


@pytest.fixture
def app_env(api: DiscordAPI) -> dict[str, str]:
    return {'CHANNEL_BACKENDS': 'fake_discord:new_channel', 'FAKE_DISCORD_URL': api.url}


def wait_for(api: DiscordAPI, chat_id: str, needle: str) -> str:
    def found() -> str | None:
        return next((t for t in api.texts(chat_id) if needle in t), None)

    return eventually(found, timeout=WAIT, what=f'a message with {needle!r}')


def say(gateway: FakeGateway, text: str, name: str = 'direct_message', **changes: object) -> dict[str, object]:
    """Pat writes to the bot directly (or as the recorded message `name` and `changes` say), over the gateway."""
    sent = message(text, name=name, **changes)
    gateway.dispatch('MESSAGE_CREATE', sent)
    return sent


def chat_runs(database_url: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute('SELECT count(*) FROM sammy.channel_runs').fetchone()
    assert row is not None
    return int(row[0])


def connected(gateway: FakeGateway, identifies: int) -> None:
    eventually(lambda: len(gateway.identifies) == identifies or None, timeout=WAIT, what=f'Identify {identifies}')


def test_a_direct_message_an_approval_a_dropped_connection_and_a_server_thread(
    app: App, client: Client, api: DiscordAPI, gateway: FakeGateway, database_url: str
) -> None:
    connected(gateway, 1)
    client.sign_up()
    code = client.http.post('/api/channels/discord/code', json={}).json()['code']
    say(gateway, f'/start {code}')
    wait_for(api, DM_ID, 'Linked. Say hi.')

    # A run in a direct message.
    say(gateway, 'Say hello')
    wait_for(api, DM_ID, 'Hello! I am Sammy.')
    thread_id = next(t['id'] for t in client.http.get('/api/threads').json() if t['title'] == 'Say hello')

    # An approval comes with buttons.
    say(gateway, 'Every Monday at 9, fill my cart at http://shop.test')

    def with_buttons() -> Call | None:
        return next((c for c in api.messages(DM_ID) if c.json().get('components')), None)

    asked = eventually(with_buttons, timeout=WAIT, what='the approval buttons')
    row = asked.json()['components']
    assert isinstance(row, list)
    buttons = row[0]['components']  # pyright: ignore[reportUnknownVariableType]
    assert [b['label'] for b in buttons] == ['Approve', 'Decline']  # pyright: ignore[reportUnknownVariableType]

    # The connection drops in the middle of the conversation. The click made meanwhile arrives after the resume, and
    # the message before it comes again too (as when a drop cuts its handling short): it still counts once.
    gateway.replay_last = True
    gateway.drop()
    pressed = click(str(buttons[0]['custom_id']), api.message_id(asked))  # pyright: ignore[reportUnknownArgumentType]
    gateway.dispatch('INTERACTION_CREATE', pressed)
    eventually(lambda: gateway.resumes or None, timeout=WAIT, what='a resume')
    path = f'/channels/{DM_ID}/messages/{api.message_id(asked)}'
    edited = eventually(lambda: api.called('PATCH', path) or None, timeout=WAIT, what='the ask edited')
    assert str(edited[0].json()['content']).endswith('You approved.') and edited[0].json()['components'] == []
    assert [c.json() for c in api.called('POST', f'/interactions/{pressed["id"]}/')] == [{'type': 6}]
    reply = client.wait_for_reply(thread_id)
    wait_for(api, DM_ID, reply[:60])
    assert len(gateway.identifies) == 1
    assert chat_runs(database_url) == 2 and BUSY not in api.texts(DM_ID)

    # In a server: chatter is not for Sammy; a mention is answered in a thread made from it, where no mention is
    # needed any more.
    gateway.dispatch('MESSAGE_CREATE', payload('server_chatter', id=new_id()))
    mention = payload('server_mention', id=new_id(), content=f'<@{BOT_ID}> Say hello')
    gateway.dispatch('MESSAGE_CREATE', mention)
    thread = str(mention['id'])
    wait_for(api, thread, 'Hello! I am Sammy.')
    say(gateway, 'Ask me my favourite colour', name='server_mention', channel_id=thread, mentions=[])
    wait_for(api, thread, 'What is your favourite colour?')
    assert api.texts(CHANNEL_ID) == []
    assert len(api.called('POST', f'/channels/{CHANNEL_ID}/messages/{thread}/threads')) == 1


def test_of_two_replicas_only_one_connects_and_the_other_takes_over(
    app: App, api: DiscordAPI, gateway: FakeGateway, tmp_path: Path
) -> None:
    connected(gateway, 1)
    replica = App(env={**app.env, 'EXECUTOR_ID': 'replica-2'}, log=tmp_path / 'replica-2.log')
    replica.start()
    try:
        time.sleep(8)  # longer than a follower waits between tries for the lock
        assert len(gateway.identifies) == 1 and gateway.connected

        app.kill()  # the leader crashes: its lock goes with its database connection
        connected(gateway, 2)
        stranger = say(gateway, 'Say hello', channel_id='1300000000000000099', author={'id': '1200000000000000099'})
        wait_for(api, str(stranger['channel_id']), '/#/link/')
        assert len(gateway.identifies) == 2
    finally:
        replica.stop()
