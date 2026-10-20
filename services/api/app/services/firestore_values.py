"""Convert nested Python values to and from Firestore-compatible shapes."""

from __future__ import annotations

from typing import Any


_NESTED_ARRAY_MARKER = "rooflineNestedArray"


def firestore_safe_value(value: Any) -> Any:
    """Encode nested arrays through maps because Firestore rejects arrays in arrays."""
    if isinstance(value, dict):
        return {key: firestore_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        encoded = [firestore_safe_value(item) for item in value]
        return [
            {_NESTED_ARRAY_MARKER: item} if isinstance(item, list) else item
            for item in encoded
        ]
    return value


def restore_firestore_value(value: Any) -> Any:
    """Restore nested arrays encoded by :func:`firestore_safe_value`."""
    if isinstance(value, dict):
        if set(value) == {_NESTED_ARRAY_MARKER} and isinstance(
            value[_NESTED_ARRAY_MARKER], list
        ):
            return [restore_firestore_value(item) for item in value[_NESTED_ARRAY_MARKER]]
        return {key: restore_firestore_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_firestore_value(item) for item in value]
    return value
