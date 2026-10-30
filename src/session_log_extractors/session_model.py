"""Adapter-to-exporter data contract, close to raw-session-log/1 JSON.

Adapters supply complete JSON-compatible interpreted content, including full
tool results. No presentation state or export limits belong in these records.
Optional keys retain their missing/null/empty meanings. The exporter reads the
fields in the current JSON schema and preserves nested event payloads. Additional
session semantics remain available on the model for other consumers.
"""

from typing import Literal, Never, NotRequired, Required, TypeAlias, TypedDict

from session_log_extractors.json_types import JsonValue
from session_log_extractors.question_types import QuestionOption
from session_log_extractors.tool_inputs import ToolInput, ToolInputSchema


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
    field_name: NotRequired[str]
    recorded_answer: NotRequired[str | list[str]]


class PlanDecision(TypedDict):
    """A proposed plan and any recorded approval, edit, or rejection."""

    plan: str
    decision: str
    edited_plan: str
    message: str


class PermissionDenial(TypedDict):
    """A normalized refusal summary attached to its evidenced tool call.

    This asserts a denial, unlike PermissionDecision's lifecycle status and
    native result label. Adapters must establish denial and call ownership from
    recorded evidence; a native decision label alone does not create this record.
    """

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
    originating_message_id: str
    interaction_id: str
    turn_id: str
    agent_id: str


class ToolResult(NativeIdentity):
    """A recorded result, associated by native call ID when available."""

    tool_call_id: str | None
    ts: str | None
    content: NotRequired[JsonValue]
    is_error: bool


class ToolExecution(NativeIdentity, total=False):
    """A recorded start of a tool execution, distinct from its request.

    Identity, timestamp, name, and input describe the execution-start record.
    A harness that does not record a start leaves ToolCall.execution absent;
    adapters must not synthesize a start from the request or result.
    """

    ts: str | None
    name: str
    input: ToolInput


class PermissionDecision(TypedDict):
    """A permission request's lifecycle, independent of normalized denial facts.

    Status records whether a completion was observed. native_decision retains
    the harness's result label verbatim, including unfamiliar labels; None
    means no label was recorded. Neither field asserts approval, denial, or an
    author. tool describes the requested action and need not be a tool name.
    Request IDs belong to the source's permission lifecycle, not to tool calls.
    ts is the completion time, or the request time when pending or untimed.
    """

    tool: str
    request_id: str | None
    status: Literal["pending", "completed"]
    native_decision: str | None
    feedback: str
    ts: str | None


class PlanStep(TypedDict):
    """One recorded plan step and its native status."""

    step: str
    status: JsonValue


class PlanUpdate(TypedDict):
    """A structured update_plan call interpreted in Plan mode."""

    explanation: str
    steps: list[PlanStep]


class FileChange(TypedDict):
    """A recorded file change, preserving content and diff as distinct facts."""

    path: str
    change_type: str | None
    content: NotRequired[str]
    unified_diff: NotRequired[str]
    move_path: NotRequired[str | None]


class PatchResult(NativeIdentity):
    """A native patch completion, without guessed tool ownership."""

    ts: str | None
    file_changes: list[FileChange]
    stdout: JsonValue
    stderr: JsonValue


class ToolCall(TypedDict, total=False):
    """A complete call and the decisions/results belonging to that call."""

    name: Required[str]
    native_success: bool
    shell_exit_code: int
    native_id: str
    plan_update: PlanUpdate
    id: str
    input: ToolInput
    input_schema: ToolInputSchema
    result: JsonValue
    result_bytes: int
    result_truncated: bool
    is_error: bool
    error: JsonValue
    result_record: ToolResult
    result_images: list[Image]
    execution: ToolExecution
    questions: list[QuestionAnswer]
    plan_decision: PlanDecision
    permission_denial: PermissionDenial
    skill: str


AuthorshipSource: TypeAlias = Literal[
    "candidate", "harness", "ide", "other_agent", "unknown"
]


class AuthorshipMarker(TypedDict):
    """A recorded source/type field, retaining its native path and JSON value."""

    field: str
    value: JsonValue


class AuthorshipRange(TypedDict):
    """Evidence-based authorship of a half-open code-point range in message text.

    Ranges are ordered and non-overlapping; uncovered text is unknown. The
    optional timestamp belongs to this content, never an inferred nearby turn.
    See scripts/fixtures/README.md for boundary and missing-evidence semantics.
    """

    start: int
    end: int
    source: AuthorshipSource
    markers: NotRequired[list[AuthorshipMarker]]
    ts: NotRequired[str | None]


class MessageContent(NativeIdentity, total=False):
    """Content and native identity shared by ordered conversation messages."""

    i: Required[int]
    ts: str | None
    text: str | None
    images: list[Image] | None
    authorship: list[AuthorshipRange]
    collaboration_mode: str
    permission_denial: PermissionDenial


class SkillInvocation(TypedDict):
    """A recorded expansion of an explicitly selected native skill."""

    name: str
    path: str


class UserMessage(MessageContent):
    """A recorded user input, with command actions recognized explicitly."""

    role: Literal["user"]
    tool_calls: NotRequired[Never]
    command: NotRequired[str]
    command_args: NotRequired[str | None]
    shell_command: NotRequired[str]
    skill_invocation: NotRequired[SkillInvocation]
    shell_result: NotRequired[ToolResult]


class AssistantMessage(MessageContent):
    """An assistant message with its own ordered tool calls."""

    role: Literal["assistant"]
    tool_calls: NotRequired[list[ToolCall]]


class Event(MessageContent, total=False):
    """Compatibility record for adapters still migrating to typed messages."""

    role: Required[str]
    tool_calls: list[ToolCall] | None


Message: TypeAlias = UserMessage | AssistantMessage | Event


class FileActivityProvenance(TypedDict):
    """Native evidence for file activity, independent of shared message order.

    record_type identifies the harness-qualified source table or record kind.
    turn_index is a recorded source index; it never indexes Session.events.
    first_seen_at is a source-reported first-seen time, not a modification time.
    Missing source fields remain None.
    """

    record_type: str
    turn_index: int | None
    first_seen_at: str | None


class FileActivity(TypedDict):
    """Evidence that a named tool touched a path, retaining its native provenance.

    Activity alone establishes neither a read/write action nor a modification.
    files_changed holds modification facts established separately by adapters.
    """

    file_path: str
    tool_name: str
    provenance: FileActivityProvenance


class Session(TypedDict):
    """Interpreted session supplied directly to the shared exporter."""

    harness: str
    session_id: str
    parent_session_id: NotRequired[str]
    events: list[Message]
    cwd: NotRequired[str | None]
    started_at: NotRequired[str | None]
    models: NotRequired[list[str] | None]
    skills_used: NotRequired[list[str]]
    subagent_count: NotRequired[int]
    unmatched_tool_results: NotRequired[list[ToolResult]]
    unsupported_tools: NotRequired[list[str]]
    permission_decisions: NotRequired[list[PermissionDecision]]
    files_changed: NotRequired[list[str]]
    file_activity: NotRequired[list[FileActivity]]
    patch_results: NotRequired[list[PatchResult]]
