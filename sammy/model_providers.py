"""Where models come from: Pydantic AI names, the Pydantic AI Gateway, and the operator's OpenAI-compatible servers.

The operator configures every endpoint (`Settings.endpoints`); users only pick among the models they offer. Their
models are discovered once, when the app starts, and only those that accept tools are offered: Sammy cannot work
without tools. Keys go to the servers and nowhere else: not into traces, logs or the picker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import httpx
import logfire
from pydantic_ai.models import Model

from sammy.imports import import_object
from sammy.observability import timing
from sammy.settings import Endpoint, Settings
from sammy.vendor.clai2_models import vllm
from sammy.vendor.clai2_models.key_profiles import gateway_model

CLAUDE_CODE_PREFIX = 'claude-code:'

PROBE_TOOL = {
    'type': 'function',
    'function': {'name': 'ping', 'description': 'Check the connection.', 'parameters': {'type': 'object'}},
}
"""What a model must accept to be offered. Servers without tool calling reject requests that carry tools."""


def load_model(name: str) -> Model | str:
    """A model name for Pydantic AI, `claude-code:NAME` for a Claude Code subscription model, or
    `script:module:attribute` for a `Model` object (or a function making one)."""
    if name.startswith(CLAUDE_CODE_PREFIX):
        from sammy.vendor.claude_code import ClaudeCodeModel

        return ClaudeCodeModel(name.removeprefix(CLAUDE_CODE_PREFIX))
    if not name.startswith('script:'):
        return name
    obj = import_object(name.removeprefix('script:'))
    return cast(Model, obj) if isinstance(obj, Model) else cast(Callable[[], Model], obj)()


class Providers:
    """The deployment's models: which users may pick, and how to build one from its name."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoints = {endpoint.name: endpoint for endpoint in settings.endpoints}
        self.discovered: tuple[str, ...] = ()

    @property
    def default(self) -> str:
        return self.settings.model

    @property
    def choices(self) -> tuple[str, ...]:
        """`ALLOWED_MODELS` (or `MODEL`), then what the operator's servers serve."""
        return tuple(dict.fromkeys((*self.settings.model_choices, *self.discovered)))

    def model(self, name: str) -> Model | str:
        prefix, _, model_name = name.partition(':')
        key = self.settings.pydantic_ai_gateway_api_key
        if prefix.startswith('gateway/') and key is not None:
            return gateway_model(name, key=key.get_secret_value())
        endpoint = self.endpoints.get(prefix)
        if endpoint is not None:
            return vllm.model(model_name, url=endpoint.base_url, token=secret(endpoint))
        return load_model(name)

    async def discover(self) -> None:
        """Ask each server for its models. One that cannot answer offers none until the app restarts."""
        found = await asyncio.gather(*(discover(endpoint) for endpoint in self.endpoints.values()))
        self.discovered = tuple(name for names in found for name in names)


async def discover(endpoint: Endpoint) -> list[str]:
    """`endpoint`'s models that accept tools, as `<endpoint>:<model>`."""
    with timing('model.discover') as span:
        span.set_attribute('endpoint', endpoint.name)  # its URL can hold a host name, its key is never here
        try:
            names = await vllm.discover(endpoint.base_url, token=secret(endpoint))
        except vllm.DiscoveryError as error:
            logfire.warn('Model discovery failed for {endpoint}: {reason}', endpoint=endpoint.name, reason=str(error))
            return []
        if endpoint.models is not None:
            names = [name for name in names if name in endpoint.models]
        accepted = await asyncio.gather(*(calls_tools(endpoint, name) for name in names))
        offered = [f'{endpoint.name}:{name}' for name, ok in zip(names, accepted, strict=True) if ok]
        span.set_attribute('models', offered)
        return offered


async def calls_tools(endpoint: Endpoint, name: str) -> bool:
    """Whether `name` accepts a request with a tool. A model or server without tool calling refuses it."""
    token = secret(endpoint)
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    body = {
        'model': name,
        'messages': [{'role': 'user', 'content': 'Call the ping tool.'}],
        'tools': [PROBE_TOOL],
        'max_tokens': 16,
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
        try:
            response = await client.post(f'{endpoint.base_url}/chat/completions', json=body, headers=headers)
        except httpx.HTTPError:
            return False
    return response.is_success


def secret(endpoint: Endpoint) -> str | None:
    return endpoint.api_key.get_secret_value() if endpoint.api_key is not None else None
