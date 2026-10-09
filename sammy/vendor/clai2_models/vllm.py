"""Discover and build models on a trusted OpenAI-compatible server, such as vLLM, Ollama or a proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field, HttpUrl, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIChatModel


class DiscoveryError(Exception):
    """The server could not be asked for its models, or did not answer with a model list."""


class ServedModel(BaseModel):
    """One OpenAI-compatible discovery result."""

    id: str = Field(min_length=1)


class ModelList(BaseModel):
    """Validated discovery response."""

    data: list[ServedModel]


def api_url(value: str) -> str:
    """Accept a server root or API root, but not credentials or query parameters."""
    try:
        url = TypeAdapter(HttpUrl).validate_python(value.strip())
    except ValidationError:
        raise ValueError('Enter a valid HTTP(S) server URL.') from None
    if url.username or url.password or url.query or url.fragment:
        raise ValueError('Use an HTTP(S) server URL without credentials, query, or fragment.')
    root = str(url).rstrip('/')
    return root if root.endswith('/v1') else root + '/v1'


async def discover(url: str, *, token: str | None) -> list[str]:
    """Query only the requested endpoint; do not forward credentials across redirects."""
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        try:
            response = await client.get(f'{api_url(url)}/models', headers=headers)
            response.raise_for_status()
        except httpx.HTTPError:
            raise DiscoveryError('Model discovery failed. Check the server URL, token, and connectivity.') from None
    try:
        names = sorted({model.id for model in ModelList.model_validate_json(response.content).data})
    except ValidationError:
        raise DiscoveryError('The server returned an invalid model list.') from None
    if not names:
        raise DiscoveryError('The server returned no models.')
    return names


def model(name: str, *, url: str, token: str | None) -> OpenAIChatModel:
    """`name` on the server at `url`, without global API-key fallbacks."""
    from pydantic_ai.models.openai import OpenAIChatModel  # settings import this module; keep that light
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(base_url=api_url(url), api_key=token or 'not-required')
    return OpenAIChatModel(name, provider=provider)
