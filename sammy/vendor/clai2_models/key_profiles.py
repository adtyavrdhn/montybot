"""The Pydantic AI Gateway branch of clai2's key profiles: a `gateway/` route with an explicit key."""

from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider

# Its overloads name the Groq, Bedrock and Google SDKs, which Sammy does not install.
from pydantic_ai.providers.gateway import gateway_provider  # pyright: ignore[reportUnknownVariableType]


def build_provider(provider: str, *, key: str) -> Provider[object]:
    """The provider core would build for `gateway/<provider>:` models, with `key` instead of the environment's key.

    It goes through core's gateway provider, as `infer_provider` does, so it keeps the Gateway's endpoint.
    """
    if not provider.startswith('gateway/'):
        raise ValueError(f'Not a Pydantic AI Gateway route: {provider}')
    return gateway_provider(provider.removeprefix('gateway/'), api_key=key)


def gateway_model(name: str, *, key: str) -> Model:
    """A `gateway/<provider>:<model>` name as a model that authenticates with `key`."""
    return infer_model(name, provider_factory=lambda provider: build_provider(provider, key=key))
