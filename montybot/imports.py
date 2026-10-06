"""Load an object named `module:attribute`, for settings that pick an implementation by name."""

from __future__ import annotations

import importlib
from typing import Any


def import_object(path: str) -> Any:
    module_name, sep, attribute = path.partition(':')
    if not sep or not module_name or not attribute:
        raise ValueError(f'{path!r} must look like "package.module:attribute"')
    obj: Any = importlib.import_module(module_name)
    for part in attribute.split('.'):
        obj = getattr(obj, part)
    return obj
