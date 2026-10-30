# Extraction information contract

## Scope

All three CLIs now emit JSON only and no longer load external skill/command
definitions or generate Markdown/image descriptions. The remaining structured
interpretation and fixture acceptance requirements below are the broader
pipeline contract; this cleanup does not claim they are all implemented.

Preserve **all existing JSON fields and contents**, plus the Markdown-only information required for the frontend behavior described in this discussion. The frontend inventory determines those additions only. It does not narrow the existing JSON contract.

## Full preservation contract

| Information | Required contents |
| --- | --- |
| Session context | `schema`, `harness`, `session_id`, `cwd`, `started_at`, and the complete `models` list, including models the UI does not display. |
| Conversation | Ordered `events` with `i`, `ts`, `role`, and `text`. Preserve message contents, code, slash/shell-command text, follow-ups, resumed activity, and image/tool attribution. Markdown within message text remains content. |
| Message authorship | Distinguish candidate-authored text from harness, IDE, and other-agent content. Preserve the candidate's words when a message contains both. For entirely injected activity, retain its timestamp and enough source/type information to display an activity marker without attributing the injected words to the candidate. |
| Tool activity | Every `tool_calls` entry's `name`, full `input`, matching `result`, `result_bytes`, `result_truncated`, and `is_error` where currently emitted. Preserve repeated calls, missing versus empty results, and all nested payload content. |
| Truncation | Raise the default tool-result limit from 4,096 to **8,192 bytes (8 KiB)** across all three harnesses. Preserve `truncation.tool_result_max_bytes`, configured overrides, original result byte counts, truncation indicators, valid UTF-8, and unlimited-result support. |
| Images | Ordered `images` with base64 `data`, `bytes`, and `media_type`, or `unavailable` and `ref`. Preserve image count and connection to the owning message. |
| Questions and answers | Question text, offered options/descriptions, recorded recommendation markers, selected answers, free-text answers, and refusals, associated with the correct question and turn. A recommended option is not evidence that it was selected. |
| Permission denials | Recognize denials across supported native wording and record formats. Preserve the denied action, any candidate feedback, and whether the refusal came from the candidate, settings, or an unknown source, associated with the relevant activity. A settings refusal must not become a candidate decision. |
| Plan decisions | Proposed plan, presentation for approval, approval/rejection, rejection feedback, and the edited version before approval. Preserve the current ExitPlanMode behavior and underlying calls/results. Missing or unknown decisions must not become approvals. |
| File actions | Action names and file paths needed for the existing “Agent actions” display, including any currently supplied only through Markdown. Preserve edit/patch contents already in JSON payloads. An attempted edit must not be described as confirmed without evidence. |
| Subagent tasks | Task description/prompt needed for “Agent actions,” plus all task inputs/results already in JSON. Preserve the full recorded prompt where available; display shortening belongs in the frontend. |
| Skills and commands | Invocation names, slash-command text/arguments, and skill-link syntax/targets needed for existing badges. Preserve skill details already contained in messages or tool inputs/results. |

Preserve answers and refusals as distinct candidate decisions. Preserve candidate-authored feedback separately from harness-generated explanations. Tool errors must not become candidate decisions.

Preserve recorded question–answer associations through extraction. Match answers using those associations. Matching counts alone does not establish a relationship; when an association is unavailable, preserve the answer without inventing one.

Required decision information must be machine-readable from JSON without reconstructing Markdown. Extract questions, feedback, and plan versions before tool-result truncation so the raw-result cap cannot erase information required above. Exact additional JSON fields remain an implementation decision.

Keep the existing warning when the complete serialized JSON exceeds **8 MiB**, including image payloads. The 8 KiB limit applies to raw tool-result text; images, inputs, conversation, and separately preserved decisions still contribute to total upload size. It is not a whole-file size guarantee.

## Markdown-only information to retire

The frontend inventory establishes no requirement to add:

- A general planning-mode lifecycle, a separate permission approval/pending lifecycle, or dedicated human-run shell labels.
- Separate changed-file lists/diff models, child-conversation exports, parent/child graphs, or subagent tool-count summaries.
- External skill/command definition loading and separate definition metadata.
- Repository/branch metadata, source-provided session summaries, references, or checkpoints.

These exclusions apply to additional extraction/enrichment only. Any such content already present in JSON stays. Markdown layout, duplicate summary/detail views, and display-time formatting also retire; source timestamps stay.

## Fixture acceptance

Complete native captures retain session files, related database rows, sidecars, and assets with structure-preserving redaction. Every contract row and supported native variant needs capture-backed coverage.

CI generates candidate JSON for human review. Ordinary CI compares complete outputs from fixed captures with approved baselines, including existing JSON fields and additions. It never auto-approves output. A separate fresh-capture workflow checks upstream drift; synthetic tests supplement native evidence.

Before implementation ships, verify that JSON alone supports the existing frontend transcript, badges, and raw-log inspection, and update consumer/CLI integration.

JSON-only uploads must handle invalid logs and valid-but-empty logs explicitly. Invalid logs should show an actionable error; valid logs with no displayable events should show an empty-session message. Neither should silently produce a blank transcript or require Markdown. Preserve existing handling for historical Markdown uploads.


## Shared text authorship contract

`MessageContent.authorship` is an optional ordered list of `AuthorshipRange`
records over the message's **unchanged `text`**. This shared-model contract is
adopted by Claude's production user-message interpreter and serialized by the
shared exporter when supplied. Codex/Copilot classification, schema versioning,
and runtime validation remain follow-ups (QUAL-2521/2522 and QUAL-2533). The
examples below illustrate the shared contract, not classifications all harnesses
can establish from their native recordings.

| Field | Meaning |
| --- | --- |
| `start`, `end` | Required half-open Unicode code-point offsets: `text[start:end]` in Python. Not UTF-8 bytes, UTF-16 code units, or grapheme clusters. Require `0 <= start < end <= len(text)`. Ranges are ordered by `start` and must not overlap; adjacent ranges may have different evidence. Do not normalize text, remove wrappers, trim whitespace, or concatenate records to establish offsets. |
| `source` | Required `candidate`, `harness`, `ide`, `other_agent`, or `unknown`. Candidate means the assessed human's input and supported native notices of their deliberate control actions, including user interruptions, shell-mode input, human-origin command invocations, native model-selector and CLI compaction invocations, and output explicitly linked to candidate-run shell commands, model selections, or compaction; native notice/wrapper/output wording need not be literally typed. Harness means injected CLI/runtime content; IDE means editor-supplied context; other_agent means another agent's contribution. Role alone, including `user`, never establishes source. Ordinary assistant output may remain unknown in this candidate-authorship contract; do not label it other-agent solely because its role is assistant. |
| `markers` | Optional list of `AuthorshipMarker` records: `field` identifies the native source/type field path (for example `isMeta` or `payload.type`); `value` preserves its exact recorded JSON value, including null or structured values. Retain multiple relevant fields separately. Markers are evidence, not universal classification rules: a wrapper-looking string alone is insufficient proof. Missing or empty means no such evidence was retained; neither implies candidate authorship. |
| `ts` | Optional verbatim timestamp recorded for the range's content. Missing means no range timestamp was supplied; null retains an explicitly unavailable timestamp. The owning message's `ts` stays unchanged. A message-level timestamp may be reused only when the record establishes it applies to that content; never infer a range timestamp from a nearby message, text mentioning a time, or chronological proximity. Different ranges may retain different recorded times. |

Missing `authorship`, an empty list, an explicit `unknown` range, and uncovered
text all mean unknown authorship for the affected text. Omission preserves
backward compatibility and is never shorthand for candidate. The field itself
is not nullable. Partial evidence is allowed: only label ranges supported by
evidence. An entirely injected message retains its full original text, message
timestamp and source/type markers so a consumer can render an activity marker.
Do not insert a synthetic candidate message or discard the injected words.

Absent, null, or empty `text` has no valid nonempty text ranges; omit `authorship`
or use `[]`. Image-only activity still retains message identity, timestamps,
image ordering, and image ownership, but text ranges do not assign image
provenance. This contract leaves image authorship unspecified. Message IDs,
ordering, and tool calls/results remain on their canonical owning records;
authorship never reassigns tools or creates question/decision associations.
No summary flag duplicates information derived from the ranges.

The six examples below show candidate-only, mixed, entirely injected, explicitly
unknown, missing evidence, and partial coverage. They are illustrative contract
examples, not native captures. `Hi 🙂` has four code points; the emoji occupies
one range position. All six have user role to demonstrate role independence.

```json
[
  {"i": 0, "role": "user", "text": "Hi 🙂", "authorship": [
    {"start": 0, "end": 4, "source": "candidate"}
  ]},
  {"i": 1, "role": "user", "ts": "2026-10-14T20:27:53.744Z", "text": "Hi 🙂<notice>context</notice>", "authorship": [
    {"start": 0, "end": 4, "source": "candidate"},
    {"start": 4, "end": 28, "source": "harness", "markers": [{"field": "block.type", "value": "notice"}], "ts": "2026-10-14T20:27:53.743Z"}
  ]},
  {"i": 2, "role": "user", "ts": "2026-10-14T20:27:53.744Z", "text": "IDE context", "authorship": [
    {"start": 0, "end": 11, "source": "ide", "markers": [{"field": "source", "value": "editor"}]}
  ]},
  {"i": 3, "role": "user", "text": "Unattributed", "authorship": [
    {"start": 0, "end": 12, "source": "unknown"}
  ]},
  {"i": 4, "role": "user", "text": "Missing evidence"},
  {"i": 5, "role": "user", "text": "Known unknown", "authorship": [
    {"start": 0, "end": 5, "source": "candidate"}
  ]}
]
```

### Native evidence and limits

The focused model tests project native records into the shared types. Claude's
fixture-backed interpretation and CLI tests additionally exercise its production
classifier and export path. Each fixture's linked provenance distinguishes
captures from hand-authored cases.

| Harness | Committed evidence and interpretation inspected | Contract fit and gaps |
| --- | --- | --- |
| Claude | [Provenance](claude/README.md): complete interactive Claude Code 2.1.312/2.1.314 capture, 2026-10-30. Typed/queued input records carry human origin; injected skill/command records carry `isMeta`; a task notification has an explicit system origin. A queued-command attachment records its original prompt, rendered wrapper, source UUID, and content timestamp. | Production tests establish candidate input and interruption actions, mixed candidate/harness, entirely injected, and unknown cases without changing text, IDs, image/tool ownership, or timestamps. Literal XML in typed input stays candidate text. SDK/human-origin alone does not establish candidate authorship. The compaction completion notice retains candidate attribution through its recorded parent-ID link. All reviewed activity in this pinned capture has resolved attribution; unsupported or ambiguous boundary cases retain unknown. Plain and wrapped native CLI `/compact` invocations count as candidate control activity; unmarked SDK prompts remain unknown. Native standalone `/model` invocations and their ID-linked result notices count as candidate control activity. A native command invocation with human origin and human turn counts as candidate activity, including its arguments. Standalone native shell-mode input and its ID-linked output count as candidate activity; unlinked output stays unknown. Peer origins were observed in separate local main logs; peer precedence/body matching is a controlled boundary test, not native coverage from this capture. IDE attribution remains unestablished. |
| Codex | [Provenance](codex/README.md): one complete interactive CLI 0.166.0 capture, 2026-10-30, with a separate native child sidecar. Native message/turn IDs and content-kind provenance survive sanitization. | Human text retains literal XML; native skill expansions retain their own identities. Recorded user-role context is not automatically candidate-authored. The child history boundary excludes inherited parent activity. Older absent-provenance cases remain focused compatibility tests. |
| Copilot | [Provenance](copilot/README.md): CLI 1.0.104-3, 2026-10-30; the versioned interactive capture retains `data.content`, `data.transformedContent`, message IDs, inline child `agentId`, native answers and permission decisions. Interpretation preserves literal human markup and ID-matched tool activity, with DB turns as fallback. | The transformed prompt supports focused mixed-range projection; native user-shell command/output ranges are separate. Child agent identity is recorded explicitly. System and usage records remain in the fixture, while unrelated history is excluded. Native IDE authorship remains unestablished. |

Shared-model examples include illustrative partial coverage and IDE sources.
Claude's peer-origin cases and missing/ambiguous boundaries are explicitly
controlled mutations of the current capture; Unicode and timestamp tests also
retain these boundaries through export. The Codex and Copilot evidence was
refreshed interactively. Tests do not establish runtime rejection of malformed
ranges. Adapters must retain complete text before assigning offsets and use
native source evidence, rather than role, tag spelling alone, or timestamp
proximity.

## Shared questions and responses contract

`Session.interaction_questions` and `Session.question_responses` are optional,
ordered lists of separate `Question` and `QuestionResponse` records. These are
shared-model fields ready for adapter adoption, not production JSON additions.
The current exporter continues to read only its existing schema and leaves
these records intact on the source model, even when it truncates raw results.
`QuestionAnswer` and `ToolCall.questions` remain migration debt for
QUAL-2523/QUAL-2524/QUAL-2525; do not populate both representations in new consumers.

| Record/field | Meaning |
| --- | --- |
| `Question.ref`, `QuestionResponse.ref` | Required local occurrence references, unique within their respective session lists. Never deduplicate repeated text or repeated native IDs. List order preserves occurrences, not a newly inferred chronology. |
| `native_id` | Optional verbatim source-recorded question, response, or option ID. Omit when absent; never substitute a local ref or tool ID. Native question IDs may be scoped to a call and reused later. |
| `Question.text`, `header`, `options` | Full question text, optional recorded header, and ordered offered `QuestionChoice` records. `options: []` allows a free-text question. No truncation, label cleanup, or inferred header. |
| `QuestionChoice.ref`, `label`, `description` | Required local option occurrence ref (unique within its question) and full recorded label, with optional recorded description. Repeated labels remain distinct options. Optional `QuestionChoice.preview` retains the complete recorded offered preview separately from its description. |
| `recommendation_markers` | Optional list of `QuestionEvidence` records identifying recorded recommendation fields or label markers. Each has a native `field` path and exact recorded JSON `value`. Keep the complete label, including any suffix. Missing/empty means no retained recommendation evidence, not a recorded negative. A recommendation or default/preselected UI option is never a selection. |
| `multiple_selections` | Optional recorded boolean indicating whether multiple selections were offered. Missing means unknown capability, not false; the response still retains every recorded selection. |
| `QuestionResponse.question` | Required `QuestionAssociation` or explicit null for unassociated responses. An association has a local `question_ref` and nonempty `evidence` of recorded IDs or an explicit native structural relationship. This addresses an occurrence, not a question string or native ID alone. |
| `activity` | Optional `QuestionActivityReference` on a question or response. `message_i` addresses canonical `Message.i`; optional zero-based `tool_call_index` addresses that message's ordered calls (including the call's matching result). Nonempty `evidence` establishes the address through native containment or recorded identifiers. Omit when unknown; a response may have an evidenced activity while its particular question remains unassociated. |
| `outcome` | Required evidence-based `answered`, `refused`, or `unknown`. Unknown means a response is present but its meaning cannot be established. A question with no associated response is unanswered **in the observed log**; this does not prove a person ignored it. Never synthesize an empty answer or refusal from silence, tool failure, missing results, or unfamiliar labels. |
| `selections` | Optional ordered `QuestionSelection` records with full recorded `label` and optional `option_ref` scoped to the associated question. Retain multiple/repeated selections. An option ref requires native selection evidence establishing the offered option; substring similarity is insufficient. Unassociated or ambiguous selections retain labels without option refs. Missing means unknown/not supplied; `[]` means explicitly no selected options. |
| `selected_preview` | Optional full preview recorded with the response, independently of offered `QuestionChoice.preview` values. Never synthesize it by copying an offered option preview; a selection does not prove a preview was returned. |
| `free_text` | Optional complete answer text, distinct from selected labels. May coexist with selections; an empty recorded string stays empty. Do not turn arbitrary raw tool text into a human answer. |
| `refusal` | Required for `outcome: refused`, otherwise absent. `QuestionRefusal.by` is `candidate`, `settings`, `harness`, or `unknown`, based on recorded evidence. Optional `explanation` retains the refusal explanation, independently of its author. Tool errors remain on canonical calls/results, never automatic candidate refusals. This is not the permission/plan contract. |
| `candidate_feedback` | Optional full feedback known to be candidate-authored. Keep harness-generated refusal explanations in `refusal.explanation`; unknown authorship does not justify candidate feedback. |

All optional fields are non-nullable unless explicitly stated above. Missing
session lists mean this interpretation was not supplied; empty lists mean no
such records were retained. These are type and semantic contracts, not runtime
validators; schema/version handling remains QUAL-2533. Response timestamps
remain QUAL-2499; source timestamps stay on canonical records.

Association evidence names source paths and retains recorded values, including
structured JSON when native containment establishes the relationship. For ID
joins, retain both sides and the question's call scope. For explicit textual
relationships, retain the containing call relationship and question text; if
repeated identical questions make the occurrence ambiguous, leave the response
unassociated. Matching counts, list positions alone, nearest timestamps, the
most recent human message, and shared labels do not establish associations.
Never pair lists with `zip` to resolve missing evidence. Local addresses identify
records only after evidence establishes ownership; they are not evidence.

Keep complete raw tool inputs/results on canonical calls and unmatched results
in `Session.unmatched_tool_results`. Structured facts are an additional semantic
view, not replacement raw payloads or a second conversation. Preserve all
unknown payload fields. Questions, selections, refusals, and feedback must be
interpreted before applying result limits. Consumers may derive interaction
groups from the references; they must not reassign canonical messages/tools.

The illustrative JSON below covers repeated questions, a recommended option
that was not selected, multiple selections plus free text, refusal with separate
feedback, an unanswered question, unassociated answers, an unknown response,
and an unrelated tool error. The `native.*` paths are illustrative evidence,
not claims about any harness's schema. These are proposed model shapes only.

```json
{
  "harness": "illustrative", "session_id": "questions-example",
  "events": [{"i": 0, "role": "assistant", "tool_calls": [
    {"name": "unrelated-tool", "result": "Tool failed", "is_error": true}
  ]}],
  "interaction_questions": [
    {"ref": "q1", "native_id": "request-a", "text": "Choose?", "multiple_selections": true, "options": [
      {"ref": "o1", "label": "A (Recommended)", "description": "First option", "recommendation_markers": [
        {"field": "native.options[0].label", "value": "A (Recommended)"}
      ]},
      {"ref": "o2", "label": "B"}, {"ref": "o3", "label": "C"}
    ]},
    {"ref": "q2", "native_id": "request-b", "text": "Choose?", "options": []},
    {"ref": "q3", "text": "Anything else?", "options": []}
  ],
  "question_responses": [
    {"ref": "r1", "outcome": "answered", "question": {"question_ref": "q1", "evidence": [
      {"field": "native.question.id", "value": "request-a"},
      {"field": "native.answer.question_id", "value": "request-a"}
    ]}, "selections": [{"option_ref": "o2", "label": "B"}, {"option_ref": "o3", "label": "C"}], "free_text": "Also keep this text"},
    {"ref": "r2", "outcome": "refused", "question": {"question_ref": "q2", "evidence": [
      {"field": "native.question.id", "value": "request-b"},
      {"field": "native.refusal.question_id", "value": "request-b"}
    ]}, "refusal": {"by": "candidate", "explanation": "Prompt dismissed"}, "candidate_feedback": "Please ask later"},
    {"ref": "r3", "outcome": "answered", "question": null, "selections": [], "free_text": "Unassociated answer"},
    {"ref": "r4", "outcome": "unknown", "question": null}
  ]
}
```

### Question fixture evidence and limits

The focused tests project fixture facts into shared records; they do not fix
native parsers or assert adapter conformance. This change generates no paid captures.

| Harness | Evidence and interpretation inspected | Fit and gaps |
| --- | --- | --- |
| Claude | [Provenance](claude/README.md): the complete `2.1.312/native/` capture, recorded interactively on 2026-10-30 with 2.1.312/2.1.314, supplies `AskUserQuestion` calls, options/descriptions/previews, multi-question/multi-select answers, notes-only and selection-with-notes responses, free text, and cancellation. `tool_use.id` / `tool_result.tool_use_id` scope `toolUseResult.answers` and `annotations` keyed by question text. | Local refs need no invented native question IDs. Capture-backed projections preserve offered previews separately from recorded selected previews and candidate notes separately from free-text answers. Cancellation has `toolDenialKind: user-rejected` as well as `is_error: true`. Parser integration remains QUAL-2523; this contract does not change the current combined-record interpretation. Recommendations, repeated identical questions, and unknown/unassociated responses remain evidence gaps. |
| Codex | [Provenance](codex/README.md): the complete interactive CLI 0.166.0 capture (2026-10-30) supplies two-question calls, recommendation suffixes, whole-label selections, selection-plus-note, custom Unicode/punctuation and cancellation. Recorded `call_id` scopes answer maps keyed by native question IDs; map order differs from question order. | Contract tests select an explicit whole-label answer using the current `codex_capture.question_interaction` helper, preserving the complete multi-question input/result. They join by question ID, never map position. `user_note:` entries and the Other sentinel remain raw native content for harness-specific interpretation; they are not additional selected options. Multi-select and previews remain unestablished. Adapter migration remains QUAL-2524. |
| Copilot | [Provenance](copilot/README.md): the complete interactive CLI 1.0.104-3 capture (2026-10-30) supplies `human_response.recorded` answer maps linked by `toolCallId`, single/multiple selections, separate free-text schema answers, selection-plus-note and cancellation. The operator-marked shell exit 7 retains wrapper `success: true`. | Source property keys establish question scope within the call. A separate free-text schema property is another question's answer, not feedback on the preceding question. Shell failure provides no candidate refusal evidence. Native previews/annotation fields remain unavailable. `copilot_fixture.native_records` selects the current source; shared-contract adoption remains QUAL-2525. |

Claude's structured `toolUseResult` is on the result entry, outside the textual
`message.content` tool-result block. Preserve that complete native result
alongside raw text; do not discard its answers or annotations. Native call IDs
and explicit answer/annotation keys establish associations. Duplicate API-request
blobs are not extra question occurrences in the canonical conversation.

For notes-only answers, the native `(notes only)` value is a harness sentinel:
retain it in raw content, use `outcome: answered`, `selections: []`, and preserve
`annotations[question].notes` as `candidate_feedback`. It is neither a chosen
label nor candidate free text. For a selection with notes, retain the recorded
label, `annotations[question].preview` as `selected_preview`, and the notes as
`candidate_feedback`; all coexist. Custom Other answers belong in `free_text`.
An explicit `toolDenialKind: user-rejected` on an ID-matched single-question call
establishes candidate refusal without invented feedback. `is_error: true` alone
does not; unrelated failed tools remain errors. Cancellation of a multi-question
call without per-question evidence must not invent individual refusals.

For example, these response fragments distinguish notes only from a selection
with its independently recorded preview (association evidence is omitted here):

```json
[
  {"ref": "notes", "question": null, "outcome": "answered", "selections": [], "candidate_feedback": "My notes"},
  {"ref": "selected", "question": null, "outcome": "answered", "selections": [{"label": "Sidebar"}], "selected_preview": "[recorded layout]", "candidate_feedback": "My notes"}
]
```

The tests select operator markers or multi-select structure, then join native
IDs; generated wording and saved IDs are not selectors. Recommendations,
unknown/unassociated responses, repeated identical questions, and refusal-source
variants beyond candidate cancellation use illustrative boundaries. Tests do
not establish rejection of malformed references, classify arbitrary native
refusal strings, or claim complete native-format coverage.
