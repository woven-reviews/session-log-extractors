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
| Truncation | The default raw-text tool-result limit is **8,192 bytes (8 KiB)** across all three harnesses. Preserve `truncation.tool_result_max_bytes`, configured overrides, original result byte counts, truncation indicators, valid UTF-8, and unlimited-result support. |
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

Each exported result string has independent original UTF-8 byte accounting and a
truncation flag. Calls use `result_bytes`/`result_truncated`; standalone result
records and attached alternate representations use
`content_bytes`/`content_truncated` and
`native_result_bytes`/`native_result_truncated`. Empty strings receive zero/false;
missing, null, objects, and lists do not receive new text accounting. Native
objects and lists, including large nested strings, remain complete. Zero limits
clip nonempty text and negative limits retain full text. Export must preserve
existing original-size and truncation provenance without mutating interpretation.

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
adopted by Claude, Codex, and Copilot and serialized by the shared exporter
when supplied. Schema versioning and general runtime validation remain
separate work (QUAL-2533). The examples below illustrate the shared contract,
not classifications all harnesses can establish from native recordings.
Python adapters use the shared `AuthorshipSource` string enum in `session_model`;
JSON retains the lowercase string values below.

| Field | Meaning |
| --- | --- |
| `start`, `end` | Required half-open Unicode code-point offsets: `text[start:end]` in Python. Not UTF-8 bytes, UTF-16 code units, or grapheme clusters. Require `0 <= start < end <= len(text)`. Ranges are ordered by `start` and must not overlap; adjacent ranges may have different evidence. Do not normalize text, remove wrappers, trim whitespace, or concatenate records to establish offsets. |
| `source` | Required `candidate`, `harness`, `ide`, `other_agent`, or `unknown`. Candidate means the assessed human's input and supported native notices of their deliberate control actions, including user interruptions, shell-mode input, human-origin command invocations, native model-selector and CLI compaction invocations, and output explicitly linked to candidate-run shell commands, model selections, or compaction; native notice/wrapper/output wording need not be literally typed. Harness means primary-assistant responses and automatically supplied CLI/runtime content; IDE means editor-supplied context; other_agent means another agent's contribution. Rendered role alone, including `user`, does not establish candidate attribution. Recorded native primary-assistant response records identify harness activity; another agent requires its separate recorded identity. Native content-kind labels such as Codex unknown do not override known response activity. |
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
| Codex | [Provenance](codex/README.md): complete interactive CLI 0.166.0 capture, 2026-10-30, and native child sidecar. Message/turn IDs, aligned block kinds/provenance, shell-command records, agent sender/recipient fields, and the child history boundary survive sanitization. | Human text (including literal XML) is candidate; environment context and skill expansions are harness. Native candidate-run shell records retain candidate attribution for the command and its recorded outcome. Explicit agent deliveries are other-agent contributions; inherited child context is excluded. Recorded attachment inputs, including their image framing, retain candidate attribution. Recorded primary-assistant responses are harness activity. Missing/unsupported/conflicting evidence stays unknown. Native IDE classification remains unestablished. |
| Copilot | [Provenance](copilot/README.md): CLI 1.0.104-3, 2026-10-30; the versioned interactive capture retains `data.content`, `data.transformedContent`, message IDs, inline child `agentId`, native answers and permission decisions. Interpretation preserves literal human markup and ID-matched tool activity, with DB turns as fallback. | Native child source markers establish other-agent input; unscoped operator inputs exercise the human default, including original-only steering. A recorded user-invoked skill in the same-agent parent-ID chain establishes candidate invocation notices and supplied arguments; loaded definition text stays harness. Full transformed text and `submitted` preserve both views. Assistant model/response-ID evidence resolves model replies as harness output, without relying on role; model generation does not imply other-agent input. Uniquely ID-linked, explicitly user-requested shell output retains candidate activity. System messages, notifications, and instruction updates retain harness provenance. All reviewed nonempty text in this capture has resolved attribution; uncertainty remains explicit in unsupported/ambiguous boundaries. Native IDE authorship remains unestablished; selection and unfamiliar-source cases are controlled boundaries. |

Shared-model examples include illustrative partial coverage and IDE sources.
Claude's peer-origin cases and missing/ambiguous boundaries are explicitly
controlled mutations of the current capture; Unicode and timestamp tests also
retain these boundaries through export. The Codex and Copilot evidence was
refreshed interactively. Tests do not establish runtime rejection of malformed
ranges. Adapters must retain complete text before assigning offsets and use
native source evidence, rather than role, tag spelling alone, or timestamp
proximity.

## Shared questions and responses contract

`ToolCall.interaction_questions` and `ToolCall.question_responses` are optional,
ordered lists of separate `Question` and `QuestionResponse` records, nested on
their owning call in `Session.events`. Claude populates and exports them without
adding top-level question/response collections. If native evidence cannot identify
a call owner, the result stays on its original message in `tool_results`, with
its `question_responses` nested on that result. Containment does not invent a
question association: an ambiguous response still has `question: null`.
The exporter preserves these structured facts even when it truncates raw text.
These separate records are the sole question interpretation in all three
harnesses. The combined `QuestionAnswer` and `ToolCall.questions` summaries
have been removed.

Codex and Copilot also keep internal `Session.interaction_questions` and
`Session.question_responses` indexes. The exporter reads facts already attached
to canonical activity; unassociated facts remain on the source model. These
indexes are not exported as top-level fields. The exporter never invents
conversation events from staged facts. Claude's orphan result events come
directly from recorded native messages.

| Record/field | Meaning |
| --- | --- |
| `Question.ref`, `QuestionResponse.ref` | Required local occurrence references, unique across their respective record types within the session. Never deduplicate repeated text or repeated native IDs. List order preserves occurrences, not a newly inferred chronology. |
| `native_id` | Optional verbatim source-recorded question, response, or option ID. Omit when absent; never substitute a local ref or tool ID. Native question IDs may be scoped to a call and reused later. |
| `Question.text`, `header`, `options` | Full question text, optional recorded header, and ordered offered `QuestionChoice` records. `options: []` allows a free-text question. No truncation, label cleanup, or inferred header. |
| `QuestionChoice.ref`, `label`, `description` | Required local option occurrence ref (unique within its question) and full recorded label, with optional recorded description. Repeated labels remain distinct options. Optional `QuestionChoice.preview` retains the complete recorded offered preview separately from its description. |
| `recommendation_markers` | Optional list of `QuestionEvidence` records identifying recorded recommendation fields or label markers. Each has a native `field` path and exact recorded JSON `value`. Keep the complete label, including any suffix. Missing/empty means no retained recommendation evidence, not a recorded negative. A recommendation or default/preselected UI option is never a selection. |
| `multiple_selections` | Optional recorded boolean indicating whether multiple selections were offered. Missing means unknown capability, not false; the response still retains every recorded selection. |
| `QuestionResponse.question` | Required `QuestionAssociation` or explicit null for unassociated responses. An association has a local `question_ref` and nonempty `evidence` of recorded IDs or an explicit native structural relationship. This addresses an occurrence, not a question string or native ID alone. |
| `activity` | Optional `QuestionActivityReference` on a question or response. `message_i` addresses canonical `Message.i`; optional zero-based `tool_call_index` addresses that message's ordered calls (including the call's matching result). Nonempty `evidence` establishes the address through native containment or recorded identifiers. Omit when unknown; a response may have an evidenced activity while its particular question remains unassociated. |
| `outcome` | Required evidence-based `answered`, `refused`, `cancelled`, or `unknown`. Unknown means a response is present but its meaning cannot be established. A question with no associated response is unanswered **in the observed log**; this does not prove a person ignored it. Never synthesize an empty answer or refusal from silence, tool failure, missing results, or unfamiliar labels. |
| `selections` | Optional ordered `QuestionSelection` records with full recorded `label` and optional `option_ref` scoped to the associated question. Retain multiple/repeated selections. An option ref requires native selection evidence establishing the offered option; substring similarity is insufficient. Unassociated or ambiguous selections retain labels without option refs. Missing means unknown/not supplied; `[]` means explicitly no selected options. |
| `selected_preview` | Optional full preview recorded with the response, independently of offered `QuestionChoice.preview` values. Never synthesize it by copying an offered option preview; a selection does not prove a preview was returned. |
| `free_text` | Optional complete answer text, distinct from selected labels. May coexist with selections; an empty recorded string stays empty. Do not turn arbitrary raw tool text into a human answer. |
| `refusal` | Required for `outcome: refused`, otherwise absent. `QuestionRefusal.by` is `candidate`, `settings`, `harness`, or `unknown`, based on recorded evidence. Optional `explanation` retains the refusal explanation, independently of its author. Tool errors remain on canonical calls/results, never automatic candidate refusals. This is not the permission/plan contract. |
| `cancellation` | Required for `outcome: cancelled`, otherwise absent. Named `QuestionCancellation` carries evidenced `by` (`candidate`, `settings`, `harness`, or `unknown`) and optional native `explanation`. Cancellation records a stopped interaction without asserting refusal or candidate feedback. |
| `candidate_feedback` | Optional full feedback known to be candidate-authored. Keep harness-generated refusal explanations in `refusal.explanation`; unknown authorship does not justify candidate feedback. |

All optional fields are non-nullable unless explicitly stated above. Missing
call/result lists mean this interpretation was not supplied; empty lists mean no
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

Keep complete raw tool inputs/results on canonical calls. Claude preserves
unmatched results in `MessageContent.tool_results` at their recorded position,
with no guessed call owner. Other harnesses retain their existing migration
boundaries. Structured facts enrich these canonical records; they do not replace
raw payloads or form a second conversation. Preserve all unknown payload fields. Questions, selections, refusals, and feedback must be
interpreted before applying result limits. Consumers may derive interaction
groups from the references; they must not reassign canonical messages/tools.

The illustrative JSON below covers repeated questions, a recommended option
that was not selected, multiple selections plus free text, refusal with separate
feedback, an unanswered question, unassociated answers, an unknown response,
and an unrelated tool error. The `native.*` paths are illustrative evidence,
not claims about any harness's schema. Questions and linked responses are on the
same tool call; orphan and unknown responses are on their native result message.

```json
{
  "harness": "illustrative",
  "session_id": "questions-example",
  "events": [
    {
      "i": 0,
      "role": "assistant",
      "tool_calls": [
        {
          "name": "question-tool",
          "interaction_questions": [
            {
              "ref": "q1",
              "native_id": "request-a",
              "text": "Choose?",
              "multiple_selections": true,
              "options": [
                {
                  "ref": "o1",
                  "label": "A (Recommended)",
                  "description": "First option",
                  "recommendation_markers": [
                    {
                      "field": "native.options[0].label",
                      "value": "A (Recommended)"
                    }
                  ]
                },
                {
                  "ref": "o2",
                  "label": "B"
                },
                {
                  "ref": "o3",
                  "label": "C"
                }
              ]
            },
            {
              "ref": "q2",
              "native_id": "request-b",
              "text": "Choose?",
              "options": []
            },
            {
              "ref": "q3",
              "text": "Anything else?",
              "options": []
            }
          ],
          "question_responses": [
            {
              "ref": "r1",
              "outcome": "answered",
              "question": {
                "question_ref": "q1",
                "evidence": [
                  {
                    "field": "native.question.id",
                    "value": "request-a"
                  },
                  {
                    "field": "native.answer.question_id",
                    "value": "request-a"
                  }
                ]
              },
              "selections": [
                {
                  "option_ref": "o2",
                  "label": "B"
                },
                {
                  "option_ref": "o3",
                  "label": "C"
                }
              ],
              "free_text": "Also keep this text"
            },
            {
              "ref": "r2",
              "outcome": "refused",
              "question": {
                "question_ref": "q2",
                "evidence": [
                  {
                    "field": "native.question.id",
                    "value": "request-b"
                  },
                  {
                    "field": "native.refusal.question_id",
                    "value": "request-b"
                  }
                ]
              },
              "refusal": {
                "by": "candidate",
                "explanation": "Prompt dismissed"
              },
              "candidate_feedback": "Please ask later"
            }
          ]
        },
        {
          "name": "unrelated-tool",
          "result": "Tool failed",
          "is_error": true
        }
      ]
    },
    {
      "i": 1,
      "role": "user",
      "tool_results": [
        {
          "tool_call_id": null,
          "ts": null,
          "is_error": false,
          "content": "Recorded response without a call owner",
          "question_responses": [
            {
              "ref": "r3",
              "outcome": "answered",
              "question": null,
              "selections": [],
              "free_text": "Unassociated answer"
            },
            {
              "ref": "r4",
              "outcome": "unknown",
              "question": null
            }
          ]
        }
      ]
    }
  ]
}
```

### Question fixture evidence and limits

The focused tests project fixture facts into shared records; they do not fix
native parsers or assert adapter conformance. This change generates no paid captures.

| Harness | Evidence and interpretation inspected | Fit and gaps |
| --- | --- | --- |
| Claude | [Provenance](claude/README.md): the complete `2.1.312/native/` capture, recorded interactively on 2026-10-30 with 2.1.312/2.1.314, supplies `AskUserQuestion` calls, options/descriptions/previews, multi-question/multi-select answers, notes-only and selection-with-notes responses, free text, and cancellation. `tool_use.id` / `tool_result.tool_use_id` scope `toolUseResult.answers` and `annotations` keyed by question text. | Local refs need no invented native question IDs. Capture-backed projections preserve offered previews separately from recorded selected previews and candidate notes separately from free-text answers. Cancellation has `toolDenialKind: user-rejected` as well as `is_error: true`. QUAL-2523 integrates these records on canonical calls/results, with native association and export tests. Recommendations, repeated identical questions, and unknown/unassociated responses remain evidence gaps. |
| Codex | [Provenance](codex/README.md): the complete interactive CLI 0.166.0 capture (2026-10-30) supplies two-question calls, recommendation suffixes, whole-label selections, selection-plus-note, custom Unicode/punctuation and cancellation. Recorded `call_id` scopes answer maps keyed by native question IDs; map order differs from question order. | Contract tests select an explicit whole-label answer using the current `codex_capture.question_interaction` helper, preserving the complete multi-question input/result. They join by question ID, never map position. Production interpretation retains `user_note:` content as `free_text`, distinct from selected labels, and excludes the custom-answer sentinel from selections when a note supplies the answer. Native cancellation is distinct from refusal. Multi-select and previews remain unestablished. |
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

### Codex production questions and responses

Codex populates both shared lists, including questions with no observed result.
The shared records are the sole interpretation; native inputs/results remain complete.
JSON places the shared facts on their existing conversation tool-call entries;
consumers continue to traverse `events` in the same order. A `request_user_input` call defines
question occurrences through its native containment. `call_id` joins results to
that call. The `answers` map uses question IDs and nested `answers` arrays;
question text and headers never establish an association. Repeated IDs across
calls remain distinct. Duplicate IDs within a call, unmatched keys, and results
with no matching call retain unassociated responses; matching counts and
timestamps never establish a link. Alternative answer containers, scalar answer
coercion, and text fallbacks are unsupported and remain raw payloads.

Native answer arrays retain whole selected labels, including punctuation and
commas, with an option reference only for one exact offered match. `user_note: `
is a native prefix: its suffix is retained verbatim as `free_text`, including
empty text. With a note, `None of the above` is a TUI custom-answer sentinel,
not a selected offered option, unless that exact option was offered. A selected
label and a note can coexist; the note is answer content, not separate feedback.
Unknown payload fields remain on complete canonical inputs/results. Unrecognized
answer values retain an `unknown` response rather than inventing an answer.

An explicit `aborted by user after <duration>s` question result is a candidate
cancellation. A multi-question call cancellation retains call activity but no
per-question association. Later user feedback stays a separate message. Older
explicit user-rejection wording is a refusal; only its `The user said:` segment
becomes candidate feedback. Error flags alone establish neither refusal nor
cancellation. Settings/permission and plan semantics remain separate.

For example, cancelling a two-question call without naming either question yields
this response (with its evidenced call activity omitted for brevity):

```json
{"ref": "codex:r0", "question": null, "outcome": "cancelled", "cancellation": {"by": "candidate", "explanation": "aborted by user after 10.2s"}}
```

The native capture verifies whole-label selections, selection-plus-note, custom
Unicode/punctuation and explicit cancellation independently of generated prose.
Controlled cases cover repeated/unmatched questions, duplicate labels, malformed
values, missing identifiers, legacy refusal feedback, and error boundaries. JSON
exports retain structured facts within their evidenced tool calls at zero, short,
and unlimited raw-result caps. Unmatched answers with no recorded call address
remain unassociated on the internal model, alongside unmatched native results.
No paid captures or consumer/timestamp changes are part of this adoption.

For example, an exported tool entry contains the facts at the point where the
interaction occurred, without new envelope-level question/response lists:

```json
{
  "name": "request_user_input",
  "input": {"questions": [{"id": "q", "question": "Choose?", "options": [{"label": "A"}]}]},
  "result": "A",
  "interaction_questions": [{"ref": "codex:q0", "native_id": "q", "text": "Choose?", "options": [{"ref": "codex:q0:o0", "label": "A"}]}],
  "question_responses": [{"ref": "codex:r0", "question": {"question_ref": "codex:q0", "evidence": [{"field": "answers key", "value": "q"}]}, "outcome": "answered", "selections": [{"label": "A", "option_ref": "codex:q0:o0"}]}]
}
```

Activity/evidence details and raw-result byte metadata are abbreviated in this
example; native payload preservation and the existing raw-result cap still apply.

## Shared permission refusals and plan decisions contract

`permission_refusals`, `plan_versions`, `plan_presentations`, and `plan_decisions`
are optional ordered lists on canonical messages, calls, or results with recorded
ownership. The three harnesses interpret and export these shared facts before
raw-result limits apply. Session-level indexes remain internal; they do not
establish ownership or create top-level export fields. The legacy `PermissionDenial`
and `PlanDecision` types and their message/call summaries have been removed.
`Session.permission_decisions` remains a distinct native request/completion
lifecycle, independent of established refusals.

| Record/field | Contract |
| --- | --- |
| `ref` | Required session-local occurrence reference, unique within each list. Repeated identical content or native IDs remain distinct occurrences. List order is not an inferred lifecycle. |
| `native_id` | Optional verbatim source identifier for that fact, never a generated ref or a borrowed tool ID. |
| `evidence` | Required nonempty list of `DecisionEvidence` records with a native field path and its complete recorded JSON value. Unknown outcome/source still requires evidence that the fact occurred. Preserve unfamiliar fields in canonical payloads; evidence is not a schema filter. |
| `activity` | Required `DecisionActivityReference` or explicit null for unknown ownership. A reference has `message_i` addressing canonical `Message.i`, optional zero-based `tool_call_index` addressing its ordered calls and matched results, and nonempty native evidence proving ownership. A message-only reference can address other recorded activity. Retain unassociated/unmatched result evidence without inventing a canonical call. |
| `PermissionRefusal.action` | Required `DeniedAction` or null when unknown. `description` retains the recorded action description; optional `tool_name` retains the native tool name when known. Full arguments stay on the canonical call, accessible through `activity`; do not replace them with this description. |
| `source` | `RefusalSource`: `candidate`, `settings`, `harness`, or `unknown`. This interpretation is separate from the native variant. Never attribute a refusal to a person solely because the call failed. |
| `native_variant` | Required exact native denial-kind string, or null when no variant was recorded. Preserve unfamiliar labels; establish denial from evidence rather than assuming every unknown result label is a refusal. |
| `explanation`, `candidate_feedback` | Separate optional complete strings on refusals and observed plan decisions. An explanation is the recorded explanation, without an authorship claim. Candidate feedback requires evidence of candidate authorship; never copy a harness explanation into it. Recorded empty strings stay empty; omit unrecorded feedback. |
| `PlanVersion.kind`, `text` | Required `PlanVersionKind` (`proposed`, `edited`, `unknown`) and full recorded plan text. A proposal alone does not prove it was presented for approval. Structured step updates remain on canonical `ToolCall.plan_update`; these versions do not create a planning-mode lifecycle. |
| `edited_from` | Optional `PlanVersionAssociation` with local `version_ref` and nonempty evidence establishing the original version. Editing is a content fact, not approval. Omit this link when the original cannot be established. Keep every version's complete text. |
| `PlanPresentation.version` | Required `PlanVersionAssociation` or null. The presentation records that a plan was offered for approval, even if its exact content cannot be associated. Repeated presentations stay distinct. A failed attempt to present a plan does not establish presentation. |
| `ObservedPlanDecision.outcome` | Required `PlanOutcome`: `approved`, `rejected`, or `unknown`. Represents an observed decision only. Missing decisions imply neither approval nor rejection; do not synthesize pending records or outcomes from silence. An unknown decision is distinct from no observed decision. |
| `native_decision` | Optional exact recorded JSON decision value, including unfamiliar strings, booleans, objects, or null. Omission means no native decision value was recorded. This value is distinct from the interpreted outcome. |
| `ObservedPlanDecision.version`, `presentation` | Required, independently nullable associations. `version` identifies the content decided on; `presentation` uses a `PlanPresentationAssociation` with `presentation_ref` identifying the review occurrence. Both require nonempty native evidence. An edited version may be approved following presentation of another version. An observed decision can remain entirely unassociated. |

Optional fields are non-nullable unless explicitly stated. Missing session lists
mean that this semantic interpretation was not supplied; empty lists mean no
such facts were retained. Types do not enforce reference integrity or validate
native payloads at runtime; that work and schema-version handling remain
QUAL-2533. Consumer decision timestamps remain QUAL-2499. Source timestamps
remain on canonical activity.

Associations require recorded IDs (retaining both sides and their call scope),
native containment, or explicit recorded relationships. Local refs merely
address facts after evidence establishes the relationship. Text equality,
similarity, nearest timestamps, most recent human messages, ordering, matching
counts, and generated assistant assertions do not establish ownership or edits.
Do not infer that later feedback refers to a cancelled permission or plan review.
Preserve complete inputs/results and unmatched results in their existing model
fields. Tool errors, cancellation, permission refusals, and plan rejection are
distinct facts; a result can support multiple facts only with explicit evidence.
Successful execution does not create a permission approval record.

This illustrative JSON is a model example, not a production export or native
capture. The `example.*` paths represent deliberately controlled source fields.
The unrelated error is not a refusal; the last version and presentation have no
decisions. The edited approval identifies a different version from the one
originally presented. No intermediate lifecycle events are necessary.

```json
{
  "harness": "illustrative", "session_id": "decisions-example",
  "events": [{"i": 0, "role": "assistant", "tool_calls": [
    {"name": "read", "is_error": true, "result": "File not found"}
  ]}],
  "permission_refusals": [
    {"ref": "d1", "action": {"description": "Write a file", "tool_name": "write"}, "source": "candidate", "native_variant": "user-rejected", "activity": null,
     "evidence": [{"field": "example.refusal", "value": {"kind": "user-rejected", "actor": "human", "feedback": "Use another file"}}],
     "explanation": "Request rejected", "candidate_feedback": "Use another file"},
    {"ref": "d2", "action": null, "source": "settings", "native_variant": "permission-rule", "activity": null,
     "evidence": [{"field": "example.refusal", "value": {"kind": "permission-rule", "denied": true}}], "explanation": "Blocked by configured rule"},
    {"ref": "d3", "action": null, "source": "unknown", "native_variant": "future-denial", "activity": null,
     "evidence": [{"field": "example.refusal", "value": {"kind": "future-denial", "denied": true}}]}
  ],
  "plan_versions": [
    {"ref": "v1", "kind": "proposed", "text": "Original café plan", "activity": null, "evidence": [{"field": "example.proposal", "value": {"id": "a", "text": "Original café plan"}}]},
    {"ref": "v2", "kind": "edited", "text": "Edited 雪 plan", "activity": null, "evidence": [{"field": "example.edit.text", "value": "Edited 雪 plan"}],
     "edited_from": {"version_ref": "v1", "evidence": [{"field": "example.edit", "value": {"original_id": "a", "edited_id": "b", "text": "Edited 雪 plan"}}]}},
    {"ref": "v3", "kind": "unknown", "text": "Another recorded plan", "activity": null, "evidence": [{"field": "example.content", "value": "Another recorded plan"}]}
  ],
  "plan_presentations": [
    {"ref": "p1", "version": {"version_ref": "v1", "evidence": [{"field": "example.review", "value": {"id": "review-a", "proposal_id": "a"}}]}, "activity": null,
     "evidence": [{"field": "example.review", "value": {"id": "review-a", "proposal_id": "a"}}]},
    {"ref": "p2", "version": null, "activity": null, "evidence": [{"field": "example.review", "value": {"id": "review-b"}}]}
  ],
  "plan_decisions": [
    {"ref": "r1", "outcome": "rejected", "native_decision": false, "activity": null,
     "version": {"version_ref": "v1", "evidence": [{"field": "example.decision", "value": {"review_id": "review-a", "version_id": "a", "approved": false}}]},
     "presentation": {"presentation_ref": "p1", "evidence": [{"field": "example.decision.review_id", "value": "review-a"}]},
     "evidence": [{"field": "example.decision.approved", "value": false}], "candidate_feedback": "Revise the plan", "explanation": "Review returned a rejection"},
    {"ref": "r2", "outcome": "approved", "native_decision": true, "activity": null,
     "version": {"version_ref": "v2", "evidence": [{"field": "example.decision", "value": {"review_id": "review-a", "version_id": "b", "approved": true}}]},
     "presentation": {"presentation_ref": "p1", "evidence": [{"field": "example.decision.review_id", "value": "review-a"}]},
     "evidence": [{"field": "example.decision.approved", "value": true}]},
    {"ref": "r3", "outcome": "unknown", "native_decision": {"kind": "future"}, "version": null, "presentation": null, "activity": null,
     "evidence": [{"field": "example.decision", "value": {"kind": "future"}}]}
  ]
}
```

### Permission and plan fixture evidence and limits

`test_session_decisions.py` checks shared-model projections from the following
committed captures and illustrative unknown/missing states. It does not assert
native adapter adoption, infer a complete lifecycle, or generate paid captures.

| Harness | Evidence and interpretation inspected | Fit and gaps |
| --- | --- | --- |
| Claude | [Provenance](claude/README.md), `2.1.312/native/` interactive capture (2026-10-30): `toolDenialKind: user-rejected` and `permission-rule` on ID-matched results; `ExitPlanMode` calls; error outside plan mode; rejection feedback; structured `toolUseResult.plan` and `planWasEdited: true`. Interpretation lives in `claude_decisions.py`. | Projections retain denial source/variant, canonical call ownership, and complete proposed/edited text compared directly with before/after editor assets. The failed presentation is a tool error, not candidate rejection. Harness-generated rejection instructions are explanations, not candidate feedback. Text-only feedback is parsed by the same shared interpreter. Unknown sources/variants use controlled boundaries. |
| Codex | [Provenance](codex/README.md), CLI 0.166.0 interactive capture (2026-10-30): `call_id`-matched permission/question cancellations (`aborted by user`), separate structured read-only sandbox refusal, proposed-plan assistant text, stay-in-Plan choice and later feedback, ordinary implementation approval. Inspected `_is_process_rejection`, `_plan_update`, and call/result interpretation. | Tests preserve complete cancellation results on canonical calls and project the separate settings refusal with its complete structured evidence. Cancellation is not the older CreateProcess denial variant; later feedback has no proven call link. The capture does not establish edited-plan editor content or `update_plan` in Plan mode. It supplies no dedicated structured decision record proving the association between ordinary implementation text and a specific plan version. The shared interpreter retains explicit candidate decisions without inventing version associations from nearby messages. |
| Copilot | [Provenance](copilot/README.md), CLI 1.0.104-3 interactive capture (2026-10-30): `permission.completed` has native kind, feedback, request/tool IDs and `decisionSource`; configured refusal is on `tool.execution_complete.error`; `human_response.recorded` contains `responseKind: exit_plan_mode`, boolean `approved`, full `planContent`, and optional feedback. Interpretation lives in `copilot_decisions.py`; native permission joins remain separate. | Projections retain native boolean outcomes, full returned plan text, independently linked presentations/versions, candidate denial feedback, and settings explanations. The response's returned plan is directly associated by containment; its equality to the initially presented version is not established. The real editor assets establish changed content in this capture, but text differences alone never prove an `edited_from` association. Permission-menu cancellation remains distinct from denial. Nonhuman actors retain unknown outcomes rather than establishing candidate approval. |

Generated plan text, feedback, and assistant prose are compared with recorded
fields or prepared editor assets rather than fixed wording. Selectors use native
tool names, denial kinds, flags and ID relationships. These tests do not classify
arbitrary denial text, prove all native variants, enforce malformed-reference
rejection, or claim that all plan presentations and decisions are recoverable.


## Shared file actions, subagent tasks, and invocations contract

`Session.file_actions`, `subagent_tasks`, and `invocations` are optional ordered
lists of typed facts for adapter adoption in QUAL-2529/QUAL-2530/QUAL-2531.
Native parsing and adapter adoption remain in those harness tickets. Shared
export supports optional `file_actions`, `subagent_tasks`, and `invocations`
attachments on messages and tool calls; existing adapters do not yet supply them.
Interpretation establishes the associations and attaches the facts. Export
preserves those attachments independently of raw-result caps; it never resolves
associations or projects these session-level lists onto the conversation.
The ordered conversation and its attachments are canonical. Session collections
are internal indexes derived from the same fact records, including unassociated
occurrences, not independently maintained interpretations. They are never
exported as top-level parallel lists.

Every fact has a session-local occurrence `ref`, optional verbatim `native_id`,
required nonempty `evidence` (`ActionEvidence`: native `field` path and complete
JSON `value`), and required nullable `activity`. Repeated identical content or
native IDs remain separate occurrences; refs do not create native evidence.
`ActionActivityReference.message_i` addresses canonical `Message.i`, not a list
position, turn index, or native message ID. Optional `tool_call_index` indexes
that message's ordered calls and ID-matched results. Its nonempty evidence must
establish ownership through native containment, recorded IDs with call scope,
or explicit native relationships. A message-only reference is valid for slash
text when the message establishes an invocation. Null preserves unknown exact
conversation location, including unmatched results; this does not imply unknown
authorship. For example, Copilot's database can establish agent file activity
without identifying the corresponding conversation call. Preserve that fact
internally with `activity: null` and do not attach or export it as a structured
fact. Existing recorded conversation content remains intact. Nearest timestamps,
most recent human messages, matching names/counts, textual similarity and assistant
claims never establish associations.

Recognized native file operations include reads, writes, edits, searches with
evidenced paths, and failed attempts. Each harness adoption documents and tests
its supported native operations. Preserve separately recorded database file
activity even without a conversation association. Opaque shell/code text does
not establish file operations, paths, or changes.

An invocation occurrence is an evidenced act of invoking a skill or command, not
each record describing it. Multiple records explicitly describing the same
invocation enrich one fact; a repeated invocation creates a separate fact even
if its name, arguments, or native ID repeat. Attach definitions and links only
when recorded evidence establishes their relationship to the invocation. A
standalone definition or link never establishes an invocation by itself. Keep
unlinked definitions as ordinary recorded content, without inventing another
invocation or associating them by matching skill names or proximity.

| Record/field | Meaning |
| --- | --- |
| `FileAction.action_name` | Exact recorded action/tool name, including unsupported names; no global cross-harness tool-name equivalence. |
| `path`, `destination_path` | Required path string or null if unknown; optional recorded move target. Preserve spelling, Unicode and relative paths without normalization. A multi-file patch can supply one fact per evidenced path with the same call activity; do not invent paths by executing opaque code. |
| `execution_outcome` | `ToolExecutionOutcome`: `succeeded`, `failed`, `unknown`. A request alone is unknown; a native success flag/exit code may establish execution outcome but does not confirm a modification. Native status values stay in complete canonical payloads/evidence. |
| `change` | Required null or `FileChangeConfirmation` with nonempty evidence establishing an actual change to this action's path. Null means no established change, including a successful no-op, read, denied/failed attempt, missing result or unsupported action. It does not assert the file was unchanged. Confirmation for one file never confirms every path in a batch. A success flag, exit code, legacy `files_changed` entry or generated assistant assertion alone is insufficient; a native structured patch or explicit recorded modification result can support confirmation. |
| `SubagentTask.prompt`, `description` | Optional independent full recorded strings. Preserve both when present; never substitute a shortened description for a prompt. Missing text is omitted and recorded empty text stays empty. Opaque/encrypted recorded strings remain opaque, with evidence of their source; no decoding or reconstructed prompt is claimed. |
| `agent_id`, `agent_type` | Optional verbatim task identity/type, only when recorded and associated. No child-conversation graph, tool counts, new diff model or task summary is introduced. |
| `Invocation.kind`, `name` | `InvocationKind`: `skill`, `command`, `unknown`; required name string or null. Unsupported invocation kinds retain evidence rather than inventing a recognized name. Invocation alone does not establish expansion, execution, completion, or human authorship. |
| `slash_text`, `arguments` | Optional complete recorded slash text and argument string; retain whitespace/Unicode and empty arguments. Do not synthesize slash syntax for a tool-invoked skill or infer arguments from a nearby message. |
| `skill_links` | Optional ordered `RecordedSkillLink` list: full recorded `syntax` and verbatim `target`. Link syntax is content, not proof of invocation or ownership by itself. |
| `definitions` | Optional ordered `InvocationDefinition` list containing full recorded `text` and optional recorded `path`. Definitions come only from messages or tool payloads already in the source. A path never authorizes external definition loading. |

Missing session lists mean interpretation was not supplied; empty lists mean no
such facts were retained. Optional fields are non-nullable unless explicitly
stated. The records describe semantics, not runtime validators; reference
integrity, schema versioning and serialization validation remain QUAL-2533.
Types do not themselves reject empty evidence or inconsistent associations.
Preserve complete edit/patch inputs, nested payloads, native success fields, and complete
results on their canonical records; retain unmatched native records separately.
Facts supplement those payloads rather than filtering them or replacing them
with display summaries. Existing `files_changed`, `FileActivity`, `PatchResult`,
`SubagentSummary`, `SkillInvocation`, command fields and `ToolCall.skill` remain
compatible. In particular, legacy `files_changed` may include attempted writes
and must not be treated as modification confirmation. New consumers should use
these facts without duplicating legacy summaries.

The harness tickets must exercise these shared acceptance examples through their
production interpretation and JSON paths wherever native evidence supports them:

- A successful edit without modification evidence has `change: null`.
- File/task facts appear on their evidenced message or call; invocation facts
  appear on their evidenced invocation message or call. Full prompts, definitions,
  and structured evidence survive a zero-byte raw textual result cap.
- One invocation and its explicitly linked definition produce one invocation fact.
  A second actual invocation remains a separate occurrence.
- A standalone definition remains recorded content without creating an invocation.
- Unlinked database file activity remains internal with `activity: null`; export
  does not guess a conversation location or attach the structured fact.
- Enrichment preserves existing message/call order and complete inputs, and export
  leaves the source model unchanged. Session indexes never cause duplicate JSON
  attachments or exporter-side association.

Shared contract tests construct these attachment shapes explicitly; they do not
claim that adapters already implement invocation recognition or association.
The shared field names and placement are settled here. Harness-owned recognition
and evidence mapping remain in QUAL-2529/2530/2531; schema-version handling and
runtime validation remain in QUAL-2533. No frontend changes, child-conversation
graphs, external definition loading, or separate diff models are introduced.

For example, this controlled message attachment is the agreed JSON placement.
The definition's parent ID proves it describes the invocation; it is not a second
invocation. Both messages keep their recorded content and order. File actions
and subagent tasks use their same-named arrays on the evidenced message or call.
The invocation's `activity` addresses `Message.i = 7`, not array position zero.

```json
{
  "events": [
    {"i": 7, "id": "m1", "role": "user", "text": "/review",
     "invocations": [{
       "ref": "v1", "kind": "command", "name": "review", "slash_text": "/review",
       "definitions": [{"text": "Recorded review instructions"}],
       "evidence": [
         {"field": "example.invocation.text", "value": "/review"},
         {"field": "example.definition.parent_id", "value": "m1"}
       ],
       "activity": {"message_i": 7, "evidence": [
         {"field": "example.invocation.id", "value": "m1"}
       ]}
     }]},
    {"i": 9, "parent_id": "m1", "role": "user", "text": "Recorded review instructions"}
  ]
}
```

This illustrative JSON is an internal model example, not native coverage or a
production export. `example.*` paths are explicitly controlled source fields.
The first write succeeds without proof of a change; the second has recorded
modification evidence. The unsupported action and task remain unassociated.
The invocation's definition and full task prompt are independent of raw results.

```json
{
  "harness": "illustrative", "session_id": "actions-example",
  "events": [{"i": 7, "role": "assistant", "tool_calls": [
    {"name": "write", "id": "c1", "input": {"path": "café.py", "content": "雪\n", "future": {"nested": [null, true]}}, "result": "Native output"}
  ]}],
  "file_actions": [
    {"ref": "a1", "action_name": "write", "path": "café.py", "execution_outcome": "succeeded", "change": null,
     "evidence": [{"field": "example.call.success", "value": true}],
     "activity": {"message_i": 7, "tool_call_index": 0, "evidence": [{"field": "example.call.id", "value": "c1"}]}},
    {"ref": "a2", "action_name": "edit", "path": "other.py", "execution_outcome": "succeeded",
     "change": {"evidence": [{"field": "example.modification", "value": {"path": "other.py", "before": "old", "after": "new"}}]},
     "evidence": [{"field": "example.action", "value": {"name": "edit", "path": "other.py"}}], "activity": null},
    {"ref": "a3", "action_name": "future-action", "path": null, "execution_outcome": "unknown", "change": null,
     "evidence": [{"field": "example.action", "value": {"name": "future-action", "unsupported": [1, null]}}], "activity": null}
  ],
  "subagent_tasks": [
    {"ref": "t1", "prompt": "Read café.py.\nPreserve this complete task, including all constraints.", "description": "Inspect file",
     "evidence": [{"field": "example.task", "value": {"description": "Inspect file", "prompt": "Read café.py.\nPreserve this complete task, including all constraints."}}], "activity": null}
  ],
  "invocations": [
    {"ref": "v1", "kind": "skill", "name": "review", "slash_text": "/review café 雪", "arguments": "café 雪",
     "skill_links": [{"syntax": "[$review](skill://review)", "target": "skill://review"}],
     "definitions": [{"text": "Complete recorded review instructions", "path": "SKILL.md"}],
     "evidence": [{"field": "example.invocation", "value": {"text": "/review café 雪", "link": "[$review](skill://review)", "definition": "Complete recorded review instructions"}}], "activity": null},
    {"ref": "v2", "kind": "command", "name": "compact", "slash_text": "/compact", "arguments": "",
     "evidence": [{"field": "example.command", "value": "/compact"}], "activity": null},
    {"ref": "v3", "kind": "unknown", "name": null,
     "evidence": [{"field": "example.invocation", "value": {"future": true}}], "activity": null}
  ]
}
```

### Action, task and invocation fixture evidence and limits

`scripts/test_session_actions.py` exercises illustrative boundaries and focused
projections from existing captures. It does not implement adapter adoption,
execute opaque code, load external definitions, or invoke paid fixture generators.

| Harness | Evidence and interpretation inspected | Fit and gaps |
| --- | --- | --- |
| Claude | [Provenance](claude/README.md), CLI 2.1.312 interactive capture and resumes: `Edit`/`Write`/`NotebookEdit`, ID-matched `tool_result`, `toolUseResult.structuredPatch` and `filePath`; `Agent.input.prompt`/`description`; `Skill` calls and command expansions. Inspected `_ClaudeInterpreter.action`, result processing, command interpretation and `summarize_subagent_source`. | Tests project full Agent text and ID-matched edits with structured native modification evidence, retaining full edit input/patch evidence. `files_changed` is populated from requests, including attempts. Skills and command bodies remain native text/payloads; this contract does not implement new parsing of their links or definitions. No exhaustive action/denial or notebook variant coverage is claimed. |
| Codex | [Provenance](codex/README.md), CLI 0.166.0 main rollout and child: `spawn_agent.arguments.message`, native call IDs, code-mode `exec` inputs containing tool use/patch text, skill selections/expansions. Inspected `call_semantics`, `patch`, command/skill recognition, `codex_sources` child boundaries and schema support. | Tests project recorded task message strings without shortening, including opaque encrypted content. Code-mode JavaScript is unsupported and stays intact; nested textual `apply_patch` is not a native call and does not establish file paths or changes. Direct patch-call coverage is a controlled boundary elsewhere, not native evidence from this capture. Custom slash arguments were rejected by the TUI; no successful native command-argument coverage is claimed. |
| Copilot | [Provenance](copilot/README.md), CLI 1.0.104-3: `tool.execution_start` create/edit arguments, ID-matched completions, `task.arguments.prompt`/`description`, `subagent.started.toolCallId`, `skill.invoked` full `content`/`path`, transformed command text and original submission. Inspected `_file_activity`, command recognition and `copilot_interactions.subagent_summaries`. | Tests project full task prompts and recorded skill definitions without filesystem loading. Native DB file activity establishes a touched path, not a confirmed modification. The existing child summary uses a description rather than the full task prompt; the new record can retain both. Custom slash invocation is a workspace skill, not a separate command mechanism. Native notebook editing is unavailable. |

Controlled tests cover successful execution without confirmation, failed/unknown
outcomes, per-path confirmation, unknown paths/names/conversation locations, independent
prompt/description absence, slash arguments, skill links, long definitions and
full prompts, nested opaque evidence, full patch input preservation and
non-mutating export at zero, short, representative and unlimited raw-result caps.
They also exercise complete message/call attachments, internal indexes without
exporter-side projection, linked definitions on a single invocation, repeated
invocations, and standalone definition content without invocation inference.
Separate Copilot database file rows are projected as internal unassociated facts.
Generated prose is compared directly with recorded fields rather than pinned
wording. Missing required native task/edit/definition evidence fails explicitly.
Link/argument boundaries and unsupported facts are illustrative, not additional
native-format coverage. Native recognition and production adapter adoption remain
in the harness tickets; the shared exporter supports the agreed attachment fields.
