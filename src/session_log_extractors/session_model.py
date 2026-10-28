"""Adapter-to-exporter data contract, close to raw-session-log/1 JSON.

Adapters supply complete JSON-compatible interpreted content, including full
tool results. No presentation state or export limits belong in these records.
Optional keys retain their missing/null/empty meanings. The exporter reads the
fields in the current JSON schema and preserves nested event payloads. Additional
session semantics remain available on the model for other consumers.
"""

from typing import Literal, NotRequired, Required, TypeAlias, TypedDict

from session_log_extractors.tool_inputs import (
    JsonValue,
    QuestionOption,
    ToolInput,
    ToolInputSchema,
)


class Image(TypedDict, total=False):
    """An inline image or an explicitly unavailable reference."""

    media_type: str
    data: str
    bytes: int
    unavailable: bool
    ref: str


class QuestionAnswer(TypedDict):
    """A question together with its options and the human's answer."""

    question: str
    header: str
    options: list[QuestionOption]
    chosen_labels: NotRequired[list[str]]
    answer_text: NotRequired[str]


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


class NativeIdentity(TypedDict, total=False):
    """Identifiers recorded by the source; absent IDs remain absent."""

    id: str
    parent_id: str
    message_id: str


class ToolResult(NativeIdentity):
    """A recorded result, associated by native call ID when available."""

    tool_call_id: str | None
    ts: str | None
    content: JsonValue
    is_error: bool


class ToolCall(TypedDict, total=False):
    """A complete call and the decisions/results belonging to that call."""

    name: Required[str]
    id: str
    input: ToolInput
    input_schema: ToolInputSchema
    result: JsonValue
    result_bytes: int
    result_truncated: bool
    is_error: bool
    error: JsonValue
    result_record: ToolResult
    questions: list[QuestionAnswer]
    plan_decision: PlanDecision
    permission_denial: PermissionDenial
    skill: str


class MessageContent(NativeIdentity, total=False):
    """Content and native identity shared by ordered conversation messages."""

    i: Required[int]
    ts: str | None
    text: str | None
    images: list[Image] | None


class UserMessage(MessageContent):
    """A recorded user input, with command actions recognized explicitly."""

    role: Literal["user"]
    command: NotRequired[str]
    command_args: NotRequired[str | None]
    shell_command: NotRequired[str]


class AssistantMessage(MessageContent):
    """An assistant message with its own ordered tool calls."""

    role: Literal["assistant"]
    tool_calls: NotRequired[list[ToolCall]]


class Event(MessageContent, total=False):
    """Compatibility record for adapters still migrating to typed messages."""

    role: Required[str]
    tool_calls: list[ToolCall] | None


Message: TypeAlias = UserMessage | AssistantMessage | Event


class Session(TypedDict):
    """Interpreted session supplied directly to the shared exporter."""

    harness: str
    session_id: str
    events: list[Message]
    cwd: NotRequired[str | None]
    started_at: NotRequired[str | None]
    models: NotRequired[list[str] | None]
    skills_used: NotRequired[list[str]]
    subagent_count: NotRequired[int]
    unmatched_tool_results: NotRequired[list[ToolResult]]
    unsupported_tools: NotRequired[list[str]]
    files_changed: NotRequired[list[str]]
