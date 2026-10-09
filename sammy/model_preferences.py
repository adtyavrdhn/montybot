"""Model picker policy. Only safe, model-supported form fields may be saved or traced.

A user's saved settings are only their overrides. Defaults are applied when a run starts, so a better default
reaches everyone who did not choose otherwise.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai.profiles.anthropic import ANTHROPIC_THINKING_BUDGET_MAP, anthropic_model_profile
from pydantic_ai.settings import ModelSettings

from sammy.model_providers import Providers
from sammy.vendor.clai2_models.model_catalog import catalog
from sammy.vendor.clai2_models.model_options import model_options, validate_model_options
from sammy.vendor.clai2_models.model_settings import ModelSettingsForm, model_defaults

# These controls override unified thinking, allow arbitrary untraceable request data, or belong to Sammy: prompt
# caching is set once for every run (`CACHE` in sammy/agent.py), so clai2's caching defaults are not used.
PRIVATE_FIELDS = frozenset(
    {
        'custom_params',
        'anthropic_cache',
        'anthropic_cache_instructions',
        'anthropic_cache_tool_definitions',
        'openai_reasoning_effort',
        'anthropic_effort',
        'anthropic_thinking_mode',
        'anthropic_thinking_budget',
        'glm_thinking',
        'glm_reasoning_effort',
    }
)
LEVELS = ('low', 'medium', 'high')


class Preference(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    model: str = Field(min_length=1, max_length=300)
    settings: dict[str, JsonValue]


class RunModel(Preference):
    """Both form and resolved wire settings are durable, not recomputed on replay."""

    resolved: dict[str, JsonValue]

    def model_settings(self) -> ModelSettings:
        # Written only by resolve(), never accepted from the API.
        return cast(ModelSettings, self.resolved)


def thinking_levels(model: str) -> list[str]:
    options = model_options(model=model)
    if 'anthropic_thinking_mode' in options:
        name = model.partition(':')[2]
        profile = anthropic_model_profile(name) or {}
        if profile.get('anthropic_supports_adaptive_thinking', False):
            return [level for level in LEVELS if level in options.get('anthropic_effort', ())]
        if name.startswith(('claude-3-7', 'claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4')):
            return list(LEVELS)
        return []
    if 'openai_reasoning_effort' in options:
        return [level for level in LEVELS if level in options['openai_reasoning_effort']]
    return []


def defaults(model: str) -> dict[str, JsonValue]:
    supported = model_options(model=model)
    values = {
        key: value
        for key, value in model_defaults(model=model).items()
        if key in supported and key not in PRIVATE_FIELDS and key != 'thinking'
    }
    levels = thinking_levels(model)
    if levels:
        values['thinking'] = 'medium' if 'medium' in levels else levels[0]
    if 'openai_text_verbosity' in supported:
        values['openai_text_verbosity'] = 'low'
    return values


def check(model: str, values: dict[str, JsonValue]) -> None:
    """Raise `ValueError` unless `model` accepts these settings, defaults included."""
    rest = dict(values)
    thinking = rest.pop('thinking', None)
    if thinking is not None and thinking not in thinking_levels(model):
        raise ValueError('Choose an available thinking level for this model.')
    validate_model_options(model=model, form=ModelSettingsForm.model_validate(rest))
    if thinking in LEVELS and 'enabled' in model_options(model=model).get('anthropic_thinking_mode', ()):
        # Validate classic thinking's sampling and token constraints after applying unified thinking.
        budget = next(value for level, value in ANTHROPIC_THINKING_BUDGET_MAP.items() if level == thinking)
        native = {**rest, 'anthropic_thinking_mode': 'enabled', 'anthropic_thinking_budget': budget}
        validate_model_options(model=model, form=ModelSettingsForm.model_validate(native))


def validate(preference: Preference, providers: Providers) -> Preference:
    """The user's overrides, checked together with the model's defaults. Raises `ValueError`."""
    if preference.model not in providers.choices:
        raise ValueError('This model is not allowed by this deployment.')
    forbidden = preference.settings.keys() & PRIVATE_FIELDS
    if forbidden:
        raise ValueError(f'Unsupported settings: {", ".join(sorted(forbidden))}.')
    check(preference.model, {**defaults(preference.model), **preference.settings})
    overrides: dict[str, JsonValue] = ModelSettingsForm.model_validate(preference.settings).model_dump(
        exclude_none=True
    )
    return Preference(model=preference.model, settings=overrides)


def resolve(preference: Preference, *, scheduled: bool = False) -> RunModel:
    """What a run uses. Scheduled runs and watches are routine, so they think `low` unless the user chose a level."""
    values = {**defaults(preference.model), **preference.settings}
    if scheduled and 'thinking' not in preference.settings and 'low' in thinking_levels(preference.model):
        values['thinking'] = 'low'
    resolved = ModelSettingsForm.model_validate(values).to_model_settings() or {}
    return RunModel(model=preference.model, settings=values, resolved=cast(dict[str, JsonValue], resolved))


def envelope(preference: Preference, providers: Providers) -> dict[str, object]:
    labels = {entry.name: entry.label for entry in catalog(include=providers.choices)}
    schema = ModelSettingsForm.model_json_schema()['properties']
    models: list[dict[str, object]] = []
    for model in providers.choices:
        options: dict[str, list[str]] = {}
        for field, choices in model_options(model=model).items():
            if field in PRIVATE_FIELDS or field == 'thinking':
                continue
            if choices:
                options[field] = list(choices)
                continue
            # Booleans and numbers stay out of this enum-only API contract.
            for variant in schema[field].get('anyOf', []):
                enum = variant.get('enum', [])
                if enum and all(isinstance(value, str) for value in enum):
                    options[field] = enum
        levels = thinking_levels(model)
        models.append(
            {
                'id': model,
                'name': labels.get(model, model),
                'thinking': levels,
                'options': options,
                'defaults': defaults(model),
            }
        )
    return {'model': preference.model, 'settings': preference.settings, 'models': models}
