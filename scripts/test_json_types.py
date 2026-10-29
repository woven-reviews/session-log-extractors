"""Checked native JSON boundaries preserve opaque payloads."""

import pytest

from session_log_extractors.json_types import json_value, parse_json, string_value


def test_json_value_copies_nested_native_payloads():
    payload = {"key": [True, None, 7, 2.5, {"unknown": "kept"}]}
    result = json_value(payload)
    assert result == payload
    assert result is not payload
    assert result["key"] is not payload["key"]


def test_json_value_rejects_non_json_objects():
    with pytest.raises(TypeError, match="JSON values"):
        json_value({1, 2})


def test_parse_json_preserves_unknown_fields_and_scalars():
    assert parse_json('{"unknown": [false, null, 1, 1.5, "value"]}') == {
        "unknown": [False, None, 1, 1.5, "value"]
    }
    assert parse_json('"opaque"') == "opaque"
    with pytest.raises(ValueError, match="Expecting"):
        parse_json("invalid")


@pytest.mark.parametrize(
    ("value", "expected"), [("text", "text"), (None, None), (7, None)]
)
def test_string_value_requires_a_recorded_string(value, expected):
    assert string_value(value) == expected
