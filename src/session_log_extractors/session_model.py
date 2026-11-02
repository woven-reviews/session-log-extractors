"""Adapter-to-exporter data contract, close to raw-session-log/1 JSON.

Adapters supply complete JSON-compatible interpreted content, including full
tool results. No presentation state or export limits belong in these records.
Optional keys retain their missing/null/empty meanings. The exporter reads the
fields in the current JSON schema and preserves nested event payloads. Additional
session semantics remain available on the model for other consumers.
"""

from enum import StrEnum
from typing import Final, Literal, Never, NotRequired, Required, TypeAlias, TypedDict

from session_log_extractors.json_types import JsonValue
from session_log_extractors.question_types import QuestionOption
from session_log_extractors.tool_inputs import ToolInput, ToolInputSchema

REFUSAL_CANDIDATE: Final = "candidate"
REFUSAL_SETTINGS: Final = "settings"
REFUSAL_UNKNOWN: Final = "unknown"


class Image(TypedDict, total=False):
    """An inline image or an explicitly unavailable reference."""

    media_type: str
    data: str
    bytes: int
    unavailable: bool
    ref: str


class QuestionAnswer(TypedDict):
    """Legacy combined record; migrate in QUAL-2523/QUAL-2524/QUAL-2525."""

    question: str
    header: str
    options: list[QuestionOption]
    chosen_labels: NotRequired[list[str]]
    answer_text: NotRequired[str]
    field_name: NotRequired[str]
    recorded_answer: NotRequired[str | list[str]]


class QuestionEvidence(TypedDict):
    """A native field path and its recorded value, not a matching heuristic."""

    field: str
    value: JsonValue


class QuestionActivityReference(TypedDict):
    """Local canonical activity address, justified by nonempty native evidence.

    message_i addresses Message.i, not a source turn or native message ID.
    tool_call_index, when present, indexes that message's ordered tool_calls.
    Omit the reference when native containment or recorded IDs do not prove it.
    """

    message_i: int
    tool_call_index: NotRequired[int]
    evidence: list[QuestionEvidence]


class QuestionChoice(TypedDict):
    """An offered option occurrence; recommendation markers never select it."""

    ref: str
    native_id: NotRequired[str]
    label: str
    description: NotRequired[str]
    preview: NotRequired[str]
    recommendation_markers: NotRequired[list[QuestionEvidence]]


class Question(TypedDict):
    """One question occurrence, independent of any observed responses.

    ref is session-local and distinct from a source-recorded native_id.
    Repeated identical text/IDs remain separate occurrences. No associated
    response means no answer was observed, never an inferred refusal.
    """

    ref: str
    native_id: NotRequired[str]
    text: str
    header: NotRequired[str]
    options: list[QuestionChoice]
    multiple_selections: NotRequired[bool]
    activity: NotRequired[QuestionActivityReference]


class QuestionAssociation(TypedDict):
    """An evidenced link to a question occurrence, never count/time matching."""

    question_ref: str
    evidence: list[QuestionEvidence]


class QuestionSelection(TypedDict):
    """A recorded selected label, with an option ref only when established."""

    label: str
    option_ref: NotRequired[str]


class QuestionResponseOutcome(StrEnum):
    """Evidence-based response states, independent of native result labels."""

    ANSWERED = "answered"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class QuestionCancellation(TypedDict):
    """An explicit cancellation, without asserting a refusal to answer."""

    by: Literal["candidate", "settings", "harness", "unknown"]
    explanation: NotRequired[str]


class QuestionRefusal(TypedDict):
    """An evidenced refusal, distinct from tool errors and candidate feedback."""

    by: Literal["candidate", "settings", "harness", "unknown"]
    explanation: NotRequired[str]


class QuestionResponse(TypedDict):
    """One observed response; None explicitly preserves unknown association.

    outcome is an evidence-based interpretation, not a native result label.
    Unknown responses preserve ambiguity; tool errors alone are not responses.
    Refused responses carry refusal; cancelled responses carry cancellation.
    free_text is answer content, whereas
    candidate_feedback contains only evidenced candidate-authored feedback.
    """

    ref: str
    native_id: NotRequired[str]
    question: QuestionAssociation | None
    outcome: QuestionResponseOutcome
    selections: NotRequired[list[QuestionSelection]]
    selected_preview: NotRequired[str]
    free_text: NotRequired[str]
    refusal: NotRequired[QuestionRefusal]
    cancellation: NotRequired[QuestionCancellation]
    candidate_feedback: NotRequired[str]
    activity: NotRequired[QuestionActivityReference]


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


class DecisionEvidence(TypedDict):
    """A source field path and its unchanged value supporting a decision fact."""

    field: str
    value: JsonValue


class DecisionActivityReference(TypedDict):
    """Canonical activity address established by nonempty native evidence.

    message_i addresses Message.i; tool_call_index indexes its ordered calls
    and their ID-matched results. Neither address nor temporal proximity proves
    ownership. Unmatched results retain their evidence without this reference.
    """

    message_i: int
    tool_call_index: NotRequired[int]
    evidence: list[DecisionEvidence]


class RefusalSource(StrEnum):
    """Evidence-based refusal source, independent of native denial labels."""

    CANDIDATE = "candidate"
    SETTINGS = "settings"
    HARNESS = "harness"
    UNKNOWN = "unknown"


class DeniedAction(TypedDict):
    """Recorded description of the denied action; full input stays on its call."""

    description: str
    tool_name: NotRequired[str]


class PermissionRefusal(TypedDict):
    """An observed denial, never an inferred approval/pending lifecycle.

    Native labels alone and tool errors do not establish denial. None preserves
    an unknown action, variant, or activity. Only evidenced candidate-authored
    feedback belongs in candidate_feedback, separately from explanation.
    """

    ref: str
    native_id: NotRequired[str]
    action: DeniedAction | None
    source: RefusalSource
    native_variant: str | None
    evidence: list[DecisionEvidence]
    activity: DecisionActivityReference | None
    explanation: NotRequired[str]
    candidate_feedback: NotRequired[str]


class PlanVersionKind(StrEnum):
    """Recorded plan content's role, not an inferred planning-mode state."""

    PROPOSED = "proposed"
    EDITED = "edited"
    UNKNOWN = "unknown"


class PlanVersionAssociation(TypedDict):
    """Link to a local version occurrence justified by nonempty native evidence."""

    version_ref: str
    evidence: list[DecisionEvidence]


class PlanVersion(TypedDict):
    """One complete recorded version; repeated identical content stays distinct.

    edited_from requires an explicit recorded relationship, not a text diff or
    adjacency. Omit it when no original version can be established. A proposed
    version does not imply presentation for approval or any observed decision.
    """

    ref: str
    native_id: NotRequired[str]
    kind: PlanVersionKind
    text: str
    edited_from: NotRequired[PlanVersionAssociation]
    evidence: list[DecisionEvidence]
    activity: DecisionActivityReference | None


class PlanPresentation(TypedDict):
    """Recorded presentation for approval, with an explicitly nullable version.

    Presentation alone is neither approval nor a planning lifecycle transition.
    """

    ref: str
    native_id: NotRequired[str]
    version: PlanVersionAssociation | None
    evidence: list[DecisionEvidence]
    activity: DecisionActivityReference | None


class PlanPresentationAssociation(TypedDict):
    """Link to a local presentation occurrence, supported by native evidence."""

    presentation_ref: str
    evidence: list[DecisionEvidence]


class PlanOutcome(StrEnum):
    """Meaning established from a recorded decision; silence has no outcome."""

    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ObservedPlanDecision(TypedDict):
    """One recorded decision, independent of versions and presentations.

    version identifies the content decided on (possibly an edited version),
    while presentation identifies the review occurrence. Null preserves either
    unknown association. native_decision retains any recorded JSON value;
    missing means no native label, not approval. Nonempty evidence supports the
    observed fact even when its meaning is unknown. Feedback authorship must be
    established separately from a harness-generated explanation.
    """

    ref: str
    native_id: NotRequired[str]
    outcome: PlanOutcome
    native_decision: NotRequired[JsonValue]
    version: PlanVersionAssociation | None
    presentation: PlanPresentationAssociation | None
    evidence: list[DecisionEvidence]
    activity: DecisionActivityReference | None
    explanation: NotRequired[str]
    candidate_feedback: NotRequired[str]


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
    parent_agent_task_id: str


class ToolResult(NativeIdentity):
    """A recorded result, associated by native call ID when available.

    content retains the result block; native_result retains an alternate native
    representation. native_denial_kind is a recorded protocol value, not a
    normalized refusal source.
    """

    tool_call_id: str | None
    ts: str | None
    content: NotRequired[JsonValue]
    native_result: NotRequired[JsonValue]
    native_denial_kind: NotRequired[str]
    question_responses: NotRequired[list[QuestionResponse]]
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
    agent_id: str
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
    interaction_questions: list[Question]
    question_responses: list[QuestionResponse]
    plan_decision: PlanDecision
    permission_denial: PermissionDenial
    skill: str


class AuthorshipSource(StrEnum):
    """Shared contribution sources; values are the stable JSON spellings."""

    CANDIDATE = "candidate"
    HARNESS = "harness"
    IDE = "ide"
    OTHER_AGENT = "other_agent"
    UNKNOWN = "unknown"


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


class SourcePosition(TypedDict, total=False):
    """Recorded editor coordinates, separate from message code-point offsets."""

    line: int
    character: int
    unrecognized_fields: dict[str, JsonValue]


class TextSelection(TypedDict, total=False):
    start: SourcePosition
    end: SourcePosition
    unrecognized_fields: dict[str, JsonValue]


class ContextAttachment(TypedDict, total=False):
    """Recorded non-image context and fields whose semantics are not yet known."""

    kind: str
    display_name: str
    path: str
    file_path: str
    text: str
    selection: TextSelection
    mention_index: int
    mime_type: str
    media_type: str
    asset_id: str
    byte_length: int
    data_url: str
    data: str
    tagged_files_entry: JsonValue
    unrecognized_fields: dict[str, JsonValue]


class SubmittedText(TypedDict):
    """Recorded pre-transformation input with its interpreted authorship."""

    text: str
    authorship: AuthorshipSource


class MessageContent(NativeIdentity, total=False):
    """Content and native identity shared by ordered conversation messages."""

    i: Required[int]
    ts: str | None
    text: str | None
    images: list[Image] | None
    authorship: list[AuthorshipRange]
    source_markers: list[AuthorshipMarker]
    context_attachments: list[ContextAttachment]
    submitted: SubmittedText
    collaboration_mode: str
    tool_results: list[ToolResult]
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
    # Codex stages these facts for projection onto evidenced calls at export.
    interaction_questions: NotRequired[list[Question]]
    question_responses: NotRequired[list[QuestionResponse]]
    permission_refusals: NotRequired[list[PermissionRefusal]]
    plan_versions: NotRequired[list[PlanVersion]]
    plan_presentations: NotRequired[list[PlanPresentation]]
    plan_decisions: NotRequired[list[ObservedPlanDecision]]
