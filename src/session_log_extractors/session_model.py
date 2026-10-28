"""Adapter-to-exporter data contract, close to raw-session-log/1 JSON.

Adapters supply complete JSON-compatible interpreted content, including full
tool results. No presentation state or export limits belong in these records.
Optional keys retain their missing/null/empty meanings. The exporter reads the
fields in the current JSON schema and preserves nested event payloads. Additional
session semantics remain available on the model for other consumers.
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


class QuestionOption(TypedDict, total=False):
    """A recorded option offered to the human."""

    label: str
    description: str


class QuestionAnswer(TypedDict):
    """A question together with its options and the human's answer."""

    question: str
    header: str
    options: list[QuestionOption]
    chosen_labels: list[str]
    answer_text: str


class PlanDecision(TypedDict):
    """A proposed plan and any recorded approval, edit, or rejection."""

    plan: str
    decision: str
    edited_plan: str
    message: str


class PermissionDenial(TypedDict):
    """A refused tool action and the recorded source of that refusal."""

    tool: str
    message: str
    by: str


class SubagentSummary(TypedDict, total=False):
    """Recorded sidecar activity available independently of JSON export."""

    agent_type: str
    start_ts: str | None
    task: str
    tool_count: int
    tool_names: list[str]
    result: str


class Interaction(TypedDict, total=False):
    """A human input and the assistant activity associated with it."""

    user_text: Required[str]
    timestamp: str | None
    images: list[Image]
    shell_command: str | None
    command: str | None
    command_args: str | None
    assistant_text_blocks: list[str]
    tool_calls: list[ToolCall]
    skills_used: list[str]
    option_qas: list[QuestionAnswer]
    permission_denials: list[PermissionDenial]
    plans: list[PlanDecision]
    subagents: list[SubagentSummary]
    subagent_count: int


class Session(TypedDict):
    """Interpreted session supplied directly to the shared exporter."""

    harness: str
    session_id: str
    events: list[Event]
    cwd: NotRequired[str | None]
    started_at: NotRequired[str | None]
    models: NotRequired[list[str] | None]
    interactions: NotRequired[list[Interaction]]
    files_changed: NotRequired[list[str]]
