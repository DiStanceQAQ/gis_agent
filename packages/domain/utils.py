from __future__ import annotations

from copy import deepcopy
from uuid import uuid4


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def merge_dicts(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result

