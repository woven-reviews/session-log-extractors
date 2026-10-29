"""Recursive JSON values shared by native readers, models, and exporters."""

import json
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


def json_value(value: object) -> JsonValue:
    """Copy a native JSON value recursively, retaining every recorded key."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    raise TypeError("Native activity must contain JSON values")


def parse_json(text: str) -> JsonValue:
    """Decode JSON and narrow the standard library's untyped result."""
    return json_value(json.loads(text))


def string_value(value: object) -> str | None:
    """Read an optional native string without coercing other recorded values."""
    return value if isinstance(value, str) else None
