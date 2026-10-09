"""Expand model request body overrides without terminal dependencies."""

from copy import deepcopy

from pydantic import JsonValue


def expand_params(*, pairs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Expand dotted keys; later values replace earlier conflicting branches."""
    body: dict[str, JsonValue] = {}
    for key, value in pairs.items():
        parts = key.split('.')
        if any(not part.strip() for part in parts):
            raise ValueError('Parameter keys need nonempty dot-separated segments.')
        target = body
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = deepcopy(value)
    return body
