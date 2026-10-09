"""Provider-name helpers only; Sammy does not manage CLAI auth profiles."""


def provider_of(model: str) -> str:
    """The provider prefix without any profile."""
    return model.partition(':')[0].partition('@')[0]


def base_model(model: str) -> str:
    """The model without its profile, for provider-specific controls and defaults."""
    prefix, separator, name = model.partition(':')
    return f'{prefix.partition("@")[0]}{separator}{name}'
