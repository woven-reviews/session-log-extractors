"""Recorded Codex rollout identity and its implications for content ownership."""

from dataclasses import dataclass
from enum import StrEnum

from session_log_extractors.codex_protocol import (
    SUBAGENT_GUARDIAN,
    THREAD_SOURCE_GUARDIAN,
    THREAD_SOURCE_SUBAGENT,
)
from session_log_extractors.json_types import JsonValue
from session_log_extractors.session_model import AuthorshipMarker


class CodexRolloutKind(StrEnum):
    """Normalized categories; missing or unfamiliar evidence remains ordinary."""

    ORDINARY = "ordinary"
    GUARDIAN = "guardian"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class CodexRolloutContext:
    """Identity established from the owning session_meta, never copied history."""

    kind: CodexRolloutKind = CodexRolloutKind.ORDINARY
    parent_session_id: str | None = None
    history_start_ordinal: int | None = None
    markers: tuple[AuthorshipMarker, ...] = ()


def recorded_ordinal(value: JsonValue) -> int | None:
    """Native ordinals are nonnegative integers, excluding JSON booleans."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def rollout_context(payload: dict[str, JsonValue]) -> CodexRolloutContext:
    """Guardian evidence wins even when the other indicator says subagent."""
    thread_source = payload.get("thread_source")
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    kind = CodexRolloutKind.ORDINARY
    if thread_source == THREAD_SOURCE_GUARDIAN or (
        isinstance(subagent, dict) and subagent.get("other") == SUBAGENT_GUARDIAN
    ):
        kind = CodexRolloutKind.GUARDIAN
    elif thread_source == THREAD_SOURCE_SUBAGENT or isinstance(subagent, dict):
        kind = CodexRolloutKind.SUBAGENT
    parent = payload.get("parent_thread_id")
    boundary = recorded_ordinal(payload.get("subagent_history_start_ordinal"))
    markers = []
    for key in (
        "thread_source",
        "source",
        "parent_thread_id",
        "subagent_history_start_ordinal",
    ):
        if key in payload:
            markers.append(
                AuthorshipMarker(
                    field=f"session_meta.payload.{key}", value=payload[key]
                )
            )
    return CodexRolloutContext(
        kind=kind,
        parent_session_id=parent if isinstance(parent, str) else None,
        history_start_ordinal=boundary if boundary else None,
        markers=tuple(markers),
    )
