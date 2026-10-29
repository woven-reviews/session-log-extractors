"""Shared question-option fields and checked narrowing without payload filtering."""

from typing import TypedDict, TypeGuard


class QuestionOption(TypedDict, total=False):
    label: str
    description: str


def is_question_option(value: object) -> TypeGuard[QuestionOption]:
    """Validate known option fields while retaining all additional recorded keys."""
    return isinstance(value, dict) and all(
        key not in value or isinstance(value[key], str)
        for key in ("label", "description")
    )
