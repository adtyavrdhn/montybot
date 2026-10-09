"""The scripted model for end-to-end tests, as in `poc/sammy_poc/remote.py`'s `scripted_shopper`.

The model reads the user's message and picks a script for it, as a real model picks a plan. A script looks only at
what its tools returned in this run, so it behaves the same whether a step ran or was replayed after a restart. The
same messages run against a real model with SAMMY_TEST_MODEL.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextContent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


@dataclass
class Turn:
    prompt: str
    returns: list[ToolReturnPart]
    instructions: str = ''
    seen: list[str] = field(default_factory=list[str])
    """Each thing the user sent in the whole conversation, as the model got it (`seen_in`)."""
    summary: str = ''
    """A long chat's summary of its oldest messages, as the model got it (`sammy.history`)."""

    @property
    def last(self) -> str:
        return str(self.returns[-1].content) if self.returns else ''

    def called(self, tool: str) -> int:
        return sum(r.tool_name == tool for r in self.returns)

    def result_of(self, tool: str) -> str:
        return next((str(r.content) for r in reversed(self.returns) if r.tool_name == tool), '')

    @property
    def url(self) -> str:
        match = re.search(r'https?://\S+?(?=[\s,.]*(?:\s|$))', self.prompt)
        assert match, f'the message names no site: {self.prompt!r}'
        return match.group(0).rstrip('/')


Script = Callable[[Turn], ModelResponse]


def call(tool: str, /, **args: object) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])


FIXTURE_CLICK = """
async def fixture_click(selector):
    page = await read_page()
    label = selector.replace('#add-', 'Add ') if selector.startswith('#add-') else {
        '#search': 'Search', '#next': 'Next page', '#place-order': 'Place order'
    }[selector]
    for line in page.splitlines():
        if ('button ' in line or 'link ' in line) and label in line:
            ref = line.split(']')[0].split('[')[1]
            return await click(ref)
    raise RuntimeError('Fixture control is not in the page')
"""


def run(code: str) -> ModelResponse:
    if 'fixture_click(' in code:
        code = FIXTURE_CLICK + code
    return call('run_code', code=code)


def say(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def line_with(page: str, needle: str) -> str:
    return next((line for line in page.splitlines() if needle in line), '')


# --- scripts, by the message they answer ---


def hello(turn: Turn) -> ModelResponse:
    return say('Hello! I am Sammy.')


def users_time(turn: Turn) -> ModelResponse:
    return say(next(line for line in turn.instructions.splitlines() if 'For the user it is now' in line))


def own_name(turn: Turn) -> ModelResponse:
    named = (line for line in turn.instructions.splitlines() if 'The user named you' in line)
    return say(next(named, 'Nobody has named me yet.'))


def favourite_colour(turn: Turn) -> ModelResponse:
    if not turn.called('ask_user'):
        return call('ask_user', question='What is your favourite colour?')
    colour = turn.result_of('ask_user')
    if not turn.called('remember'):
        return call('remember', fact=f'Favourite colour: {colour}')
    return say(f'Got it: your favourite colour is {colour}.')


def order_eggs(turn: Turn) -> ModelResponse:
    if turn.returns and turn.returns[-1].tool_name == 'run_code' and '\nError: ' in '\n' + turn.last:
        return say(f'My code failed: {turn.last}')
    if not turn.returns:
        # `shop` is used again after the hand-off: Monty keeps the session's variables across the pause.
        return run(f"shop = {turn.url!r}\nawait goto(shop + '/')\nprint(await fixture_click('#add-eggs'))")
    if 'Title: Sign in' in turn.last:
        if turn.called('hand_off'):
            return say('You are still not signed in, so I stopped.')
        return call('hand_off', reason='Please sign in to the shop, then hand the browser back.')
    if 'In cart: eggs' not in turn.last and not turn.called('commit'):
        return run("await goto(shop + '/')\nprint(await fixture_click('#add-eggs'))")
    if not turn.called('commit'):
        return call('commit', target='#place-order', description='Place the order for eggs ($3.20)')
    result = turn.result_of('commit')
    order = re.search(r'Order #\d+: [a-z, ]+, \$\d+\.\d\d', result)
    if order is None:
        return say(f'I did not place the order. {result}')
    return say(f'Done. {order.group(0)}')


# One snippet reads every page of results and works out the answer in Monty.
CHEAPEST_FLIGHTS = """
import re
page = await goto(site + '/')
page = await fixture_click('#search')
rows = []
while True:
    rows += re.findall(r'([A-Z0-9]{2} \\d{3,4})\\W+(\\d\\d:\\d\\d)\\W+€\\s?(\\d+)', page)
    if 'Next page' not in page:
        break
    page = await fixture_click('#next')
best = sorted(rows, key=lambda row: int(row[2]))[:3]
for flight, departs, price in best:
    print(f'| {flight} | {departs} | €{price} |')
"""


def cheapest_flights(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return run(f'site = {turn.url!r}' + CHEAPEST_FLIGHTS)
    return say(
        f'The three cheapest flights to Lisbon next Friday:\n\n| Flight | Departs | Price |\n|---|---|---|\n{turn.last}'
    )


def todays_offer(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return run(f'print(await goto({turn.url + "/"!r}))')
    if 'Press and hold' in turn.last and not turn.called('hand_off'):
        return call(
            'hand_off', reason='The store wants a press-and-hold check. Please hold the button, then hand back.'
        )
    offer = line_with(turn.last, 'Today only')
    return say(offer.strip(' -') if offer else f'I could not get past the check. {turn.last}')


# U5: the browser downloads the newest three CSVs into the user's files, and the same snippet reads them with pathlib.
DOWNLOAD_INVOICES = """
import re
from pathlib import Path
page = await goto(site + '/')
saved = []
for ref in re.findall(r'\\[(\\d+)\\] link "CSV"', page)[:3]:
    page = await click(ref)
    saved += re.findall(r'Downloaded: (\\S+)', page)
total = 0
for name in saved:
    for line in Path(name).read_text().splitlines()[1:]:
        item, quantity, price = line.split(',')
        total += int(quantity) * float(price)
print(f'{len(saved)} invoices, total {total:.2f}')
"""


def total_invoices(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return run(f'site = {turn.url!r}' + DOWNLOAD_INVOICES)
    found = re.search(r'(\d+) invoices, total (\d+\.\d\d)', turn.last)
    if found is None:
        return say(f'I could not total them. {turn.last}')
    return say(f'Your last {found.group(1)} invoices come to €{found.group(2)}.')


# U5 with the CPython tier (#6): Monty downloads and totals, pandas totals the same files in CPython and reads what
# Monty wrote, and Monty reads what pandas wrote.
PANDAS_TOTAL = """
import pandas as pd
from pathlib import Path
frames = [pd.read_csv(name) for name in NAMES]
total = sum((frame.quantity * frame.unit_price).sum() for frame in frames)
Path('pandas-total.txt').write_text(f'{total:.2f}')
print(f'pandas: {total:.2f}, Monty: {Path("monty-total.txt").read_text()}')
"""


def total_invoices_with_pandas(turn: Turn) -> ModelResponse:
    if not turn.returns:
        save = "\nPath('/work/monty-total.txt').write_text(f'{total:.2f}')\nprint(saved)"
        return run(f'site = {turn.url!r}' + DOWNLOAD_INVOICES + save)
    if not turn.called('run_python'):
        names = [name.removeprefix('/work/') for name in re.findall(r"'(/work/[^']+)'", turn.last)]
        return call('run_python', code=f'NAMES = {names!r}' + PANDAS_TOTAL)
    if turn.called('run_code') < 2:
        return run("from pathlib import Path\nprint('read back:', Path('/work/pandas-total.txt').read_text())")
    found = re.search(r'pandas: (\d+\.\d\d), Monty: (\d+\.\d\d)', turn.result_of('run_python'))
    if found is None:
        return say(f'I could not total them. {turn.result_of("run_python")}')
    return say(f'Your last three invoices come to €{found.group(1)} (Monty got €{found.group(2)}; {turn.last}).')


def show_file(turn: Turn) -> ModelResponse:
    """Lists the user's files, then reads the path at the end of the message, wherever it points."""
    if not turn.returns:
        path = turn.prompt.split()[-1]
        return run(
            f"from pathlib import Path\nprint(sorted(p.name for p in Path('/work').iterdir()))\n"
            f'print(Path({path!r}).read_text())'
        )
    return say(turn.last)


def describe_files(turn: Turn) -> ModelResponse:
    """Says what the model was given: everything the user sent in the conversation, one line each."""
    return say('\n'.join(turn.seen))


def share_report(turn: Turn) -> ModelResponse:
    """Makes a file with code, then gives it to the user."""
    if not turn.called('run_code'):
        return run("from pathlib import Path\nPath('/work/report.csv').write_text('item,total\\neggs,3\\n')")
    if not turn.called('share_file'):
        return call('share_file', path='report.csv')
    return say(f'Here is your report. ({turn.result_of("share_file")})')


def fail(turn: Turn) -> ModelResponse:
    raise RuntimeError('the model provider is down')


def order_from_code(turn: Turn) -> ModelResponse:
    """Tries to place the order from code, which skips the approval; the browser functions refuse."""
    if not turn.returns:
        return run(f"shop = {turn.url!r}\nawait goto(shop + '/')\nprint(await fixture_click('#add-eggs'))")
    if 'Title: Sign in' in turn.last and not turn.called('hand_off'):
        return call('hand_off', reason='Please sign in to the shop, then hand the browser back.')
    if turn.called('run_code') < 2:
        return run(
            "await goto(shop + '/')\nawait fixture_click('#add-eggs')\nprint(await fixture_click('#place-order'))"
        )
    return say(turn.last)


# --- schedules (U4) ---


def schedule(**args: object) -> Script:
    """Set up a schedule (the user approves it), then say what the tool said. The task's prompt names the site."""

    def script(turn: Turn) -> ModelResponse:
        if not turn.called('schedule_task'):
            prompt = str(args['prompt']).format(url=turn.url)
            return call('schedule_task', **{**args, 'prompt': prompt})
        return say(turn.result_of('schedule_task'))

    return script


def fill_cart(turn: Turn) -> ModelResponse:
    """A scheduled run: put eggs and milk in the cart, signing in through a hand-off only if the shop asks."""
    if not turn.returns:
        return run(f"shop = {turn.url!r}\nawait goto(shop + '/')\nprint(await fixture_click('#add-eggs'))")
    if 'Title: Sign in' in turn.last:
        if turn.called('hand_off'):
            return say('You are still not signed in, so I stopped.')
        return call('hand_off', reason='Please sign in to the shop, then hand the browser back.')
    if 'In cart: eggs, milk' not in turn.last and turn.called('run_code') < 3:
        return run(
            "for item in ('eggs', 'milk'):\n    await goto(shop + '/')\n    page = await fixture_click('#add-' + item)\nprint(page)"
        )
    return say(line_with(turn.last, 'In cart:') or f'I could not fill the cart. {turn.last}')


def check_slot(turn: Turn) -> ModelResponse:
    """A scheduled run of a watch: notify only when a slot is there."""
    if not turn.returns:
        return run(f'print(await goto({turn.url + "/"!r}))')
    slot = line_with(turn.result_of('run_code'), 'Available:')
    if not slot:
        return say('Not yet.')
    if not turn.called('notify_user'):
        return call('notify_user')
    return say(f'A delivery slot opened. {slot.strip(" -")}')


def schedule_id(turn: Turn) -> str:
    match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', turn.prompt)
    assert match, f'the message names no schedule: {turn.prompt!r}'
    return match.group(0)


def on_schedule(tool: str) -> Script:
    def script(turn: Turn) -> ModelResponse:
        if not turn.called(tool):
            return call(tool, schedule_id=schedule_id(turn))
        return say(turn.last)

    return script


def my_schedules(turn: Turn) -> ModelResponse:
    if not turn.called('list_schedules'):
        return call('list_schedules')
    return say(turn.last)


def my_linear(turn: Turn) -> ModelResponse:
    """ "yo what's on my linear": connect Linear if it is not, then read the issues."""
    if not turn.called('connect_integration'):
        return call('connect_integration', service='Linear', reason='Connect Linear so I can look up your issues.')
    connected = turn.result_of('connect_integration')
    if 'is connected' not in connected:
        return say(connected)
    if not turn.called('list_integration_tools'):
        return call('list_integration_tools', integration='linear', search='list issues')
    if not turn.called('call_integration_tool'):
        return call('call_integration_tool', integration='linear', tool='LINEAR_LIST_LINEAR_ISSUES', arguments={})
    return say(turn.last)


def new_linear_issue(turn: Turn) -> ModelResponse:
    """ "Create a Linear issue called X": a tool that changes something, so the user approves it first."""
    if not turn.called('call_integration_tool'):
        title = turn.prompt.removeprefix('Create a Linear issue called ').strip()
        return call(
            'call_integration_tool', integration='linear', tool='LINEAR_CREATE_LINEAR_ISSUE', arguments={'title': title}
        )
    return say(turn.last)


def my_notes(turn: Turn) -> ModelResponse:
    """ "What notes are in mcp:<server>": the user's own MCP server."""
    key = turn.prompt.split()[-1].rstrip('?')
    if not turn.called('list_integration_tools'):
        return call('list_integration_tools', integration=key)
    if not turn.called('call_integration_tool'):
        return call('call_integration_tool', integration=key, tool='list_notes', arguments={})
    return say(f'Your notes: {turn.last}')


def my_gmail(turn: Turn) -> ModelResponse:
    """ "Check my Gmail": connect Gmail, an app through Composio, and say how that went."""
    if not turn.called('connect_integration'):
        return call('connect_integration', service='Gmail', reason='Connect Gmail so I can check your email.')
    return say(turn.result_of('connect_integration'))


def acme_wiki(turn: Turn) -> ModelResponse:
    """A service no app is offered for: the chat offers to add an MCP server for it."""
    if not turn.called('connect_integration'):
        return call('connect_integration', service='Acme Wiki', reason='Add your Acme Wiki so I can search it.')
    return say(turn.result_of('connect_integration'))


def message_ten(turn: Turn) -> ModelResponse:
    """ "What did I say in message 10?": in a long chat only the summary has it, so read it back."""
    if not turn.called('read_history'):
        return call('read_history', start=10, end=10)
    return say(f'You said: {turn.result_of("read_history")}')


def chat_seen(turn: Turn) -> ModelResponse:
    """ "How much of our chat do you see?": how many of the user's messages, and whether a summary came with them."""
    summarised = ' and a summary of the rest' if 'Summary of previous conversation' in turn.summary else ''
    return say(f'I see {len(turn.seen)} of your messages{summarised}.')


def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return say('The user keeps notes about apples.')


summarizer = FunctionModel(summarize, model_name='scripted-summarizer')
"""Writes a long chat's summary (`HISTORY_SUMMARY_MODEL=script:e2e.scripts:summarizer`)."""


SCRIPTS: dict[str, Script] = {
    "yo what's on my linear": my_linear,
    'What did I say in message 10': message_ten,
    'How much of our chat do you see': chat_seen,
    'Search my Acme Wiki': acme_wiki,
    'Check my Gmail': my_gmail,
    'Create a Linear issue called': new_linear_issue,
    'What notes are in': my_notes,
    'Every Monday at 9, fill my cart at': schedule(
        name='Weekly groceries',
        cron='0 9 * * 1',
        timezone='Europe/London',
        when='Mondays at 09:00',
        prompt='Fill my cart at {url} with eggs and milk',
    ),
    'Fill my cart at': fill_cart,
    'Every Friday at 8, order eggs from': schedule(
        name='Weekly eggs',
        cron='0 8 * * 5',
        timezone='Europe/London',
        when='Fridays at 08:00',
        prompt='Order eggs from {url}',  # each run is `order_eggs`, which ends in `commit`
    ),
    'Tell me when a delivery slot opens at': schedule(
        name='Delivery slot',
        cron='*/30 * * * *',
        timezone='UTC',
        when='every 30 minutes',
        prompt='Check for a delivery slot at {url}',
        watch=True,
    ),
    'Check every minute for a delivery slot at': schedule(
        name='Delivery slot, every minute',
        cron='* * * * *',
        timezone='UTC',
        when='every minute',
        prompt='Check for a delivery slot at {url}',
        watch=True,
    ),
    'Check for a delivery slot at': check_slot,
    'Pause the schedule': on_schedule('pause_schedule'),
    'Resume the schedule': on_schedule('resume_schedule'),
    'Delete the schedule': on_schedule('delete_schedule'),
    'What are my schedules': my_schedules,
    'Order eggs straight from code at': order_from_code,
    'Fail please': fail,
    'Say hello': hello,
    'Ask me my favourite colour': favourite_colour,
    'What time is it for me': users_time,
    'What is your name': own_name,
    'Order eggs from': order_eggs,
    'Find the three cheapest flights to Lisbon next Friday': cheapest_flights,
    'What is on offer today at': todays_offer,
    'Download my last three invoices from': total_invoices,
    'Total my last three invoices with pandas from': total_invoices_with_pandas,
    'Show me my files and the file': show_file,
    'Describe what I attached': describe_files,
    'Make me a report': share_report,
}


def seen_in(messages: list[ModelMessage]) -> list[str]:
    """What the user sent, as the model got it, one line each (newlines as `\\n`): `text: ...`, `note: ...` for a
    file's note, `file text: ...` for a text file's contents, and `<media type> <width>x<height>` or `<media type> <bytes> bytes` for a file it sees."""
    seen: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            for item in [part.content] if isinstance(part.content, str) else part.content:
                if isinstance(item, str):
                    seen.append(f'text: {item}')
                elif isinstance(item, TextContent):
                    is_note = isinstance(item.metadata, dict) and 'attachment' in item.metadata
                    seen.append(f'note: {item.content}' if is_note else f'file text: {item.content}')
                elif isinstance(item, BinaryContent) and item.is_image:
                    with Image.open(io.BytesIO(item.data)) as image:
                        seen.append(f'{item.media_type} {image.width}x{image.height}')
                elif isinstance(item, BinaryContent):
                    seen.append(f'{item.media_type} {len(item.data)} bytes')
    return [line.replace('\n', '\\n') for line in seen]  # one line each, whatever a file's text holds


def current_turn(messages: list[ModelMessage]) -> Turn:
    """The user's latest message and the tool results since."""
    start = max(
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    )
    content = next(p.content for p in messages[start].parts if isinstance(p, UserPromptPart))  # pyright: ignore[reportAttributeAccessIssue]
    # A message with files is a list: the text first, if there is any.
    prompt = content if isinstance(content, str) else next((c for c in content if isinstance(c, str)), '')
    returns = [
        part
        for message in messages[start:]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    latest = messages[-1]
    instructions = (latest.instructions or '') if isinstance(latest, ModelRequest) else ''
    summary = '\n'.join(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    )
    return Turn(prompt=prompt, returns=returns, instructions=instructions, seen=seen_in(messages), summary=summary)


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    turn = current_turn(messages)
    for start, script in SCRIPTS.items():
        if turn.prompt.startswith(start):
            return script(turn)
    return say(f'I have no script for {turn.prompt!r}.')


model = FunctionModel(respond, model_name='scripted')
