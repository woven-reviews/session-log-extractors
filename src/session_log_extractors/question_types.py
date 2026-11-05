"""Common fields recorded in native Claude and Codex question options."""

from typing import TypedDict


class QuestionOption(TypedDict, total=False):
    label: str
    description: str
