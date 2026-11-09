"""Harness identifiers at the public command and capture boundaries."""

from enum import StrEnum


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"
