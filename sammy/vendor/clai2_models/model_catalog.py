"""Static model metadata from genai-prices and Pydantic AI, without provider discovery."""

from collections.abc import Iterable
from dataclasses import dataclass

from genai_prices.data_snapshot import get_snapshot
from pydantic_ai.models import known_model_names

from .profiles import provider_of


@dataclass(frozen=True, kw_only=True)
class CatalogModel:
    """One model with optional context and pricing metadata."""

    name: str
    provider: str
    label: str
    context_window: int | None = None
    prices: str | None = None


def runnable_providers() -> frozenset[str]:
    """Recognized provider prefixes, not a guarantee that an SDK or credentials are installed."""
    return frozenset(name.partition(':')[0] for name in known_model_names()) | {'claude-code'}


def genai_prices_models() -> list[CatalogModel]:
    """Current (not deprecated) models from genai-prices, for recognized providers."""
    providers = runnable_providers()
    found: list[CatalogModel] = []
    for provider in get_snapshot().providers:
        if provider.id not in providers:
            continue
        for model in provider.models:
            if model.deprecated or ':' in model.id:
                continue
            found.append(
                CatalogModel(
                    name=f'{provider.id}:{model.id}',
                    provider=provider.id,
                    label=model.name or model.id,
                    context_window=model.context_window,
                    prices=str(model.prices),
                )
            )
    return found


def catalog(*, include: Iterable[str] = (), discovered: Iterable[CatalogModel] = ()) -> list[CatalogModel]:
    """Merge static and supplied entries; supplied discovery replaces its providers' static entries.

    No network or authentication discovery runs here. Include Sammy's configured subscription
    model explicitly; subscription availability is account-specific.
    """
    models = {model.name: model for model in genai_prices_models()}
    for name in (*known_model_names(), *include):
        if name and name not in models:
            models[name] = CatalogModel(name=name, provider=provider_of(name), label=name.partition(':')[2])
    discovered = tuple(discovered)
    providers = {model.provider for model in discovered}
    models = {name: model for name, model in models.items() if model.provider not in providers}
    models.update((model.name, model) for model in discovered)
    return [models[name] for name in sorted(models)]
