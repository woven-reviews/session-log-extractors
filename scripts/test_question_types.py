"""Shared question-option narrowing retains additional recorded fields."""

from copy import deepcopy

import pytest

from session_log_extractors.question_types import is_question_option


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "option",
        [],
        {"label": 1},
        {"description": None},
        {"label": "x", "description": False},
    ],
)
def test_option_guard_rejects_invalid_known_fields(value):
    assert not is_question_option(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"label": "x"},
        {"description": "desc"},
        {"label": "x", "description": "desc", "preview": {"nested": [None, True]}},
    ],
)
def test_option_guard_preserves_additional_fields(value):
    original = deepcopy(value)
    assert is_question_option(value)
    assert value == original
