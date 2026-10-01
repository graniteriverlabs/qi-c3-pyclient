# client/core/app_response.py
"""
Helpers for reading the GRL app's HTTP replies.

The app accepts request bodies case-insensitively but *answers* in camelCase, regardless of the
casing we sent. Reading a reply with a case-sensitive lookup therefore reports "the app stored
nothing" for data it did in fact store — that exact mistake produced a false alarm on the optimum
coil read-back before it was caught. Anything that inspects an app reply should go through
``ci_get`` rather than indexing the dict directly.
"""
from typing import Any


def ci_get(mapping: Any, name: str, default: Any = None) -> Any:
    """
    Look a key up in an app reply without regard to case.

    Args:
        mapping: The decoded reply (or any dict); non-dicts return the default
        name: Key to find, in whatever casing the caller finds readable
        default: Returned when the key is absent

    Returns:
        The value, or `default`
    """
    if not isinstance(mapping, dict):
        return default
    if name in mapping:
        return mapping[name]
    lowered = name.lower()
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return default
