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
    description: NotRequired[str]
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
    recorded_answer preserves the native answer value before interpretation,
    including unsupported shapes. It is absent for a refusal without an answer.
    Refused responses carry refusal; cancelled responses carry cancellation.
    free_text is answer content, whereas
    candidate_feedback contains only evidenced candidate-authored feedback.
    """

    ref: str
    native_id: NotRequired[str]
    question: QuestionAssociation | None
    outcome: QuestionResponseOutcome
    recorded_answer: NotRequired[JsonValue]
    selections: NotRequired[list[QuestionSelection]]
    selected_preview: NotRequired[str]
    free_text: NotRequired[str]
    refusal: NotRequired[QuestionRefusal]
    cancellation: NotRequired[QuestionCancellation]
    candidate_feedback: NotRequired[str]
    activity: NotRequired[QuestionActivityReference]


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


class ActionEvidence(TypedDict):
    """Native field path and unchanged value supporting an action fact."""

    field: str
    value: JsonValue


class ActionActivityReference(TypedDict):
    """Canonical activity proven by recorded IDs or native containment.

    message_i addresses Message.i, not its list position. tool_call_index
    indexes that message's ordered calls and ID-matched results. Nonempty
    evidence establishes conversation association, not authorship; ordering
    and timestamps never do. A null activity on a fact leaves its exact
    conversation location unknown, even when agent activity is established.
    """

    message_i: int
    tool_call_index: NotRequired[int]
    evidence: list[ActionEvidence]


class ToolExecutionOutcome(StrEnum):
    """Observed execution outcome, independent of whether a file changed."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FileChangeConfirmation(TypedDict):
    """Nonempty evidence that this action actually changed its named path.

    A success flag, exit code, attempted write, or legacy files_changed entry
    alone is insufficient. Retain complete native results on canonical activity.
    """

    evidence: list[ActionEvidence]


class FileAction(TypedDict):
    """One evidenced action on one path; an attempt is not a confirmed change.

    action_name preserves the recorded name, including unsupported names.
    path is null when unknown; destination_path retains a recorded move target.
    change is null without evidence of an actual change, including successful
    no-ops. Confirmation for one path never confirms other paths in a patch.
    Full edits/patches remain on the canonical call, not in a new diff model.
    Recognized native file operations include reads and failed attempts.
    Separate database activity can remain internal with activity=None; opaque
    shell/code text does not establish file operations.
    """

    ref: str
    native_id: NotRequired[str]
    action_name: str
    path: str | None
    destination_path: NotRequired[str]
    execution_outcome: ToolExecutionOutcome
    change: FileChangeConfirmation | None
    evidence: list[ActionEvidence]
    activity: ActionActivityReference | None


class SubagentTask(TypedDict):
    """A recorded task occurrence with complete, separately recorded text.

    Omit missing prompt/description rather than substituting one for the other.
    agent_id is a recorded identity, never a child graph or inferred call ID.
    Inputs/results stay complete on canonical activity or unmatched records.
    """

    ref: str
    native_id: NotRequired[str]
    prompt: NotRequired[str]
    description: NotRequired[str]
    agent_id: NotRequired[str]
    agent_type: NotRequired[str]
    evidence: list[ActionEvidence]
    activity: ActionActivityReference | None


class InvocationKind(StrEnum):
    """Interpreted invocation kind, separate from the recorded name/syntax."""

    SKILL = "skill"
    COMMAND = "command"
    UNKNOWN = "unknown"


class RecordedSkillLink(TypedDict):
    """Complete recorded link syntax and its verbatim target."""

    syntax: str
    target: str


class InvocationDefinition(TypedDict):
    """Full definition already recorded in a message or native tool payload.

    path is optional recorded provenance, never an instruction to load a file.
    """

    text: str
    path: NotRequired[str]


class Invocation(TypedDict):
    """One recorded invocation, without inventing expansion or execution.

    name is null if unknown. slash_text and arguments retain exact source text,
    including empty arguments; omit absent data. Repeated invocations remain
    distinct. Links/definitions preserve recorded order and full content.
    Multiple records explicitly describing one invocation enrich that occurrence;
    a link or definition alone does not establish an invocation. Attach definitions
    only through recorded relationships; otherwise retain them as source content.
    """

    ref: str
    native_id: NotRequired[str]
    kind: InvocationKind
    name: str | None
    slash_text: NotRequired[str]
    arguments: NotRequired[str]
    skill_links: NotRequired[list[RecordedSkillLink]]
    definitions: NotRequired[list[InvocationDefinition]]
    evidence: list[ActionEvidence]
    activity: ActionActivityReference | None


class ActionFacts(TypedDict, total=False):
    """Facts attached by interpretation to their evidenced message or call.

    Export preserves these attachments without resolving session collections.
    Session collections are internal indexes of the same facts, including
    unassociated facts with activity=None; those facts are never attached or
    exported as top-level collections. Association does not establish authorship.
    """

    file_actions: list[FileAction]
    subagent_tasks: list[SubagentTask]
    invocations: list[Invocation]


class DecisionFacts(TypedDict, total=False):
    """Decision facts attached to the canonical activity carrying their evidence."""

    permission_refusals: list[PermissionRefusal]
    plan_versions: list[PlanVersion]
    plan_presentations: list[PlanPresentation]
    plan_decisions: list[ObservedPlanDecision]


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


class ToolResult(NativeIdentity, DecisionFacts):
    """A recorded result, associated by native call ID when available.

    content retains the result block; native_result retains an alternate native
    representation. native_denial_kind is a recorded protocol value, not a
    normalized refusal source.
    """

    tool_call_id: str | None
    ts: str | None
    content: NotRequired[JsonValue]
    native_result: NotRequired[JsonValue]
    # Export-generated accounting; adapters retain complete text without these fields.
    content_bytes: NotRequired[int]
    content_truncated: NotRequired[bool]
    native_result_bytes: NotRequired[int]
    native_result_truncated: NotRequired[bool]
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


class CollaborationMode(StrEnum):
    """Known normalized modes; messages may retain unfamiliar recorded modes."""

    PLAN = "plan"
    DEFAULT = "default"


class PermissionStatus(StrEnum):
    """Whether a recorded permission request has completed."""

    PENDING = "pending"
    COMPLETED = "completed"


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
    status: PermissionStatus
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


class ToolCall(DecisionFacts, ActionFacts, total=False):
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
    interaction_questions: list[Question]
    question_responses: list[QuestionResponse]
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


class MessageContent(NativeIdentity, DecisionFacts, ActionFacts, total=False):
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
    Legacy files_changed may include attempts; it is not confirmation evidence.
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
    # Internal indexes of canonical attached facts and unassociated occurrences.
    # Derive these from the same records; export never projects these lists.
    file_actions: NotRequired[list[FileAction]]
    subagent_tasks: NotRequired[list[SubagentTask]]
    invocations: NotRequired[list[Invocation]]
    # Session-wide indexes of question facts; canonical owners are calls/results.
    interaction_questions: NotRequired[list[Question]]
    question_responses: NotRequired[list[QuestionResponse]]
    permission_refusals: NotRequired[list[PermissionRefusal]]
    plan_versions: NotRequired[list[PlanVersion]]
    plan_presentations: NotRequired[list[PlanPresentation]]
    plan_decisions: NotRequired[list[ObservedPlanDecision]]
