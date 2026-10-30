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
| `source` | Required `candidate`, `harness`, `ide`, `other_agent`, or `unknown`. Candidate means the assessed human's input and supported native notices of their deliberate control actions, including user interruptions, shell-mode input, and output explicitly linked to a candidate-run shell command; native notice/wrapper/output wording need not be literally typed. Harness means injected CLI/runtime content; IDE means editor-supplied context; other_agent means another agent's contribution. Role alone, including `user`, never establishes source. Ordinary assistant output may remain unknown in this candidate-authorship contract; do not label it other-agent solely because its role is assistant. |
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
| Claude | [Provenance](claude/README.md): complete interactive Claude Code 2.1.312/2.1.314 capture, 2026-10-30. Typed/queued input records carry human origin; injected skill/command records carry `isMeta`; a task notification has an explicit system origin. A queued-command attachment records its original prompt, rendered wrapper, source UUID, and content timestamp. | Production tests establish candidate input and interruption actions, mixed candidate/harness, entirely injected, and unknown cases without changing text, IDs, image/tool ownership, or timestamps. Literal XML in typed input stays candidate text. SDK/human-origin alone and unmarked slash-command records remain unknown. Standalone native shell-mode input and its ID-linked output count as candidate activity; unlinked output stays unknown. Peer origins were observed in separate local main logs; peer precedence/body matching is a controlled boundary test, not native coverage from this capture. IDE attribution remains unestablished. |
| Codex | [Provenance](codex/README.md): CLI 0.162.1, 2026-10-15; `slash.jsonl` contains user `response_item` / `input_text` and recorded timestamp. `extract_codex_session_log.py` now assembles canonical messages and ID-matched tools from native records, cleans user wrappers, and ignores developer/system context. | Original text and timestamp fit unchanged; absent authorship remains unknown despite user role. The generator removes startup/mode instructions, so these fixtures cannot establish complete injected-context coverage or native mixed/IDE/other-agent classifications. |
| Copilot | [Provenance](copilot/README.md): CLI 1.0.90-5, 2026-10-14; `baseline/events.jsonl` has `data.content` and `data.transformedContent`, with a `<current_datetime>` prefix, message ID, and timestamp. `extract_copilot_session_log.py` now assembles canonical messages and ID-matched tools from native activity, uses DB turns as a fallback, and cleans wrapper text. | The transformed text's exact content suffix supports a mixed-range projection using the known capture prompt; preserve the prefix verbatim, never parse its displayed time as the range timestamp. Captures omit `system.message` and usage checkpoints. A shared `parentAgentTaskId` is not alone evidence of other-agent authorship. Native IDE/other-agent coverage remains unestablished. |

Shared-model examples include illustrative partial coverage and IDE sources.
Claude's peer-origin cases and missing/ambiguous boundaries are explicitly
controlled mutations of the current capture; Unicode and timestamp tests also
retain these boundaries through export. No additional capture was generated for
this change. Tests do not establish runtime rejection of malformed ranges. Adapters
must retain complete text before assigning offsets and use native source
evidence, rather than role, tag spelling alone, or timestamp proximity.
