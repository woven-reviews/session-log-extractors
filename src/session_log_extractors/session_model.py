"""Adapter-to-exporter data contract, close to raw-session-log/1 JSON.

Adapters supply complete JSON-compatible interpreted content, including full
tool results. No presentation state or export limits belong in these records.
Optional keys retain their missing/null/empty meanings; additional recorded
payload keys are passed through by the exporter without filtering.
"""

from typing import Any, NotRequired, Required, TypedDict


class Image(TypedDict, total=False):
    """An inline image or an explicitly unavailable reference."""

    media_type: str
    data: str
    bytes: int
    unavailable: bool
    ref: str


class ToolCall(TypedDict, total=False):
    """Complete interpreted tool data, before any textual result cap."""

    name: Required[str]
    input: Any
    result: Any
    result_bytes: int
    result_truncated: bool
    is_error: bool
    error: Any


class Event(TypedDict, total=False):
    """One ordered event, retaining its recorded payload and metadata."""

    i: Required[int]
    role: Required[str]
    ts: str | None
    text: str | None
    images: list[Image] | None
    tool_calls: list[ToolCall] | None


class Session(TypedDict):
    """Interpreted session supplied directly to the shared exporter."""

    harness: str
    session_id: str
    events: list[Event]
    cwd: NotRequired[str | None]
    started_at: NotRequired[str | None]
    models: NotRequired[list[str] | None]
