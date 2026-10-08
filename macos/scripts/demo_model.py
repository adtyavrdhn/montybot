"""The local demo's model: the end-to-end tests' scripted model (`tests/e2e/scripts.py`), made forgiving for a person
trying the Mac app. A message without a site gets the matching fixture site (from SAMMY_DEMO_SITES, which
dev_server.py sets), so the app's own suggestions work; a message it has no script for gets a friendly reply that
says what it can do, instead of a failed task.
"""

from __future__ import annotations

import json
import os

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from scripts import SCRIPTS, current_turn, respond

SITES: dict[str, str] = json.loads(os.environ.get('SAMMY_DEMO_SITES', '{}'))
# What a person might write, and the scripted prompt (with its site) that plays it.
TOPICS = [
    ('flight', 'Find the three cheapest flights to Lisbon next Friday at {flights}'),
    ('order', 'Order eggs from {shop}'),
    ('egg', 'Order eggs from {shop}'),
    ('cart', 'Fill my cart at {shop} with eggs and milk'),
    ('shopping', 'Every Monday at 9, fill my cart at {shop} with eggs and milk'),
    ('invoice', 'Download my last three invoices from {invoices}'),
    ('slot', 'Tell me when a delivery slot opens at {slots}'),
    ('offer', 'What is on offer today at {checkpoint}'),
    ('colour', 'Ask me my favourite colour and remember it.'),
    ('color', 'Ask me my favourite colour and remember it.'),
]
HELP = (
    "I'm the local demo, so I only know a few tasks. Try one of these:\n\n"
    '- **Find the three cheapest flights to Lisbon next Friday**: a reply with a table\n'
    '- **Order eggs**: you sign in for me (alice / hunter2), then approve the order\n'
    '- **Download my last three invoices**: then look in Files\n'
    '- **Tell me when a delivery slot opens**: a schedule\n'
    '- **Ask me my favourite colour**: a question, then Memory'
)


def rewrite(prompt: str) -> str | None:
    """The scripted prompt for what the person wrote, or None if there is none."""
    if any(prompt.startswith(start) for start in SCRIPTS) and (
        'http' in prompt or 'colour' in prompt or 'hello' in prompt.lower() or prompt.startswith('Fail')
    ):
        return prompt
    lowered = prompt.lower()
    for word, template in TOPICS:
        if word in lowered:
            return template.format(**SITES) if SITES or '{' not in template else None
    return None


def demo(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    turn = current_turn(messages)
    scripted = rewrite(turn.prompt)
    if scripted is None:
        return ModelResponse(parts=[TextPart(content=HELP)])
    if scripted != turn.prompt:
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, UserPromptPart) and part.content == turn.prompt:
                        part.content = scripted
    return respond(messages, info)


model = FunctionModel(demo, model_name='demo')
