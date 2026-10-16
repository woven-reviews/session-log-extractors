# Extraction information contract

## Scope

Plan to make JSON the sole extraction output. Implementation has not changed.

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
