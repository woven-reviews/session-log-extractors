# Copilot fixture provenance

## Current test capture

Tests explicitly select **1.0.104-3**, captured through the normal interactive
Copilot CLI on 2026-10-30. The successful main session is
`07640183-dc62-4ab4-813f-d9f285a63b90`, resumed with that exact UUID. One explore
child, `f4257c74-873d-426e-9fe3-f5718b346bbb`, is recorded inline with `agentId`.
There is no child sidecar transcript. The complete input is
[`native/session-state/<uuid>/events.jsonl`](1.0.104-3/native/session-state/07640183-dc62-4ab4-813f-d9f285a63b90/events.jsonl).

The source run is `20261030T195019Z`; detailed terminal evidence, action journal,
coverage ledger, hashes, and sanitizer counts remain untracked at
`captures/copilot/1.0.104-3/20261030T195019Z/`. Raw originals and private replacement
rules remain outside the repository. Capture ran for about 14 minutes and used
141.29 displayed AI credits, with a 500-credit soft ceiling. A sandboxed launch
failed before conversation; its provisional session was not merged. The selected
conversation used Claude Sonnet 5.5, a short GPT-6 Luna recall turn, and one
explore child using its native default model. Tests do not pin model names or
require particular generated prose.

Launches used `--no-auto-update --disable-builtin-mcps --sync-session=off`.
The first started with `--max-ai-credits 40`, changed to 500 through `/limits`.
Resume supplied `--resume <uuid> --max-ai-credits 500 --deny-tool 'shell(touch)'`
for a session-only automatic refusal. The rule expired on clean exit. No global
configuration was changed. Capture assets lived in a disposable temporary
workspace outside this repository; prompts did not discuss extraction or tests.

## Bundle and sanitization

[`native/session-store.db`](1.0.104-3/native/session-store.db) contains only this
session and its dependent native rows, preserving table DDL and recorded primary
keys. [`native/session-store.json`](1.0.104-3/native/session-store.json) preserves
the same selected rows in readable form. The global-store rows were exported
inside one read transaction; the session-local todo database used SQLite backup.
The bundle retains native workspace metadata, plans, checkpoints and relevant
rewind files. Transient locks and unrelated indexes/history were excluded.
[`assets/`](1.0.104-3/assets/) includes the local source images, long path,
multibyte text, skills/slash definitions and before/after editor plans.

Sanitization used the existing `capture_sanitization.sanitize_capture` API at
revision `10b7fba`, with explicit workspace/home/host replacements and built-in
email/key rules. A literal-only second text pass covered JSON keys. Every text
output and count report reproduced identically. SQLite was rebuilt from
sanitized rows/dumps; native IDs, timestamps, ordering and binary image bytes
were retained. Audit reports are evidence, not test fixtures. No tests access the
operator's Copilot login or invoke a paid CLI.

## Evidence and limitations

The capture supplies single-choice/overlapping-label, multi-question/multi-select,
free-text/Other and selection-plus-note responses; question cancellation; rejected,
revised, approved and genuinely editor-modified plans; human permission feedback,
pending-menu cancellation and configured refusal; native reads/writes/edits/search,
large inputs and multibyte results; empty success, successful mixed streams and
nonzero shell exit; repeated skills, slash arguments, one subagent; model change,
exact resume, compaction/recall; two same-message PNG attachments and later
JPEG/GIF/WebP attachments, plus a separate image-reading tool result.

The CLI did not expose native notebook editing or question preview/annotation
fields. Other/free text is a schema answer, not an independent annotation. Its
custom slash mechanism is a user-invoked workspace skill, not a separate command
format. Escape interrupted foreground handling but did not kill a background
shell; it later completed with exit 0. A fast navigation attempt selected an
option instead of Other; the later genuine custom answer is retained separately.
The source contains these failures without repairs. Compaction appended records
and a checkpoint; the original native history remained available.

`human_response.recorded` answer maps, joined by `toolCallId`, are authoritative
for questions; rendered comma-separated result text is not an answer parser.
The interpreted model retains agent identities, question answers, plan decisions,
refusal sources, native shell success/exit status, complete tool-result blocks,
and separately accessible child summaries. The current JSON schema omits question/plan decisions and child summaries
intentionally; it exports complete tool inputs/results/errors, ordered images,
agent scope, authorship, source/type markers, and user-shell rendered text. Native user-shell streams are
combined; separate stdout/stderr ordering cannot be recovered. Tool-result
budgets 0/17/4096/unlimited and a real multibyte split, non-mutation, and exact
selected-source CLI stdout/file parity are tested.

## Tests and focused boundaries

`scripts/copilot_fixture.py` pins this version and discovers its single transcript.
Integration, source-free contract and smoke tests use the complete capture.
Focused boundary tests select unchanged ID-linked tool evidence; the single-image
helper deliberately narrows the recorded two-image message to its red attachment.
Selectors use tool names, structured properties and operator-controlled markers.
Tests verify reworded assistant prose, question titles/options and corresponding
answer maps without approving new prose baselines. Exact expectations apply to
controlled parser examples, assets, IDs/ownership and native payload retention.

[`boundary/`](1.0.104-3/boundary/) retains explicitly controlled malformed JSONL,
legacy permission lifecycle, and legacy inline/data-URL attachment encodings
which this successful capture cannot produce. The attachment variants use the
current red PNG bytes. Tests retain controlled missing files, filesystem errors,
malformed types, unknown tools, unmatched results, missing IDs, legacy database
schemas, and collision cases.

## Capture workflow

The obsolete unversioned captures, case manifest, database, and generator assets
have been removed, together with the scripted headless generator and its fake-CLI
tests. Only the selected interactive capture and test-consumed focused boundary
inputs remain. Use the repository's capture-native-session skill for future
captures. Paid capture is explicit, bounded, and excluded from tests and CI.

## Authorship interpretation (QUAL-2522)

The PR uses this same complete interactive capture; no SDK fixtures, case
manifests, headless generators, or new capture sessions are required. Its native
evidence covers 22 user-message records: 19 with a uniquely located original in
transformed text, two original-only steering inputs, and one replaced slash
submission. One of the 19 mixed inputs has both an identified-agent `source` and
recipient `agentId`; the other operator inputs use the native unscoped human
default. System messages, a structured notification, instruction updates with
`authority: runtime`, and a user-requested shell action establish harness context
and command/output boundaries. Tests assert recorded data relationships, not
assistant wording or model names.

`text` preserves complete `transformedContent` when recorded, otherwise original
`content`. The named `submitted` record preserves original input plus its
interpreted authorship. In the slash case, the full submission does not occur in
transformed skill instructions. It survives in `submitted`; the parent-ID chain
through recorded hooks uniquely links it to `skill.invoked` with trigger
`user-invoked`. The supported invocation notice and exact supplied arguments
retain candidate activity. Loaded definition text and automatic reminders stay
harness content. This does not classify from wrapper appearance or event timing. Command/argument recognition still uses the
recorded original; wrapper-looking human text remains candidate input. Ranges
always refer to unchanged message `text`, with Unicode code-point offsets.

Source classification precedes boundary matching: explicit supported agent/system
sources override the unscoped human default, scoped records without source stay
unknown, and contradictory automatic-continuation evidence stays unknown.
`parentAgentTaskId`, delivery timing, and tag spelling alone do not establish an
author. The original-only steering inputs retain their full candidate text.
All 32 assistant prose records have `model` and `apiCallId` producer
evidence; they retain assistant role and resolved `other_agent` contribution
ranges. Assistant role or an agent ID alone is insufficient. User-shell output
retains candidate activity through the recorded unique, same-agent call ID and
`isUserRequested: true` completion (or an unambiguous direct parent link when
that optional flag is absent). An explicitly non-user completion never inherits
candidate activity. The inserted display separator shares that
outcome attribution. Ambiguous request IDs and mismatched agent scopes leave
results explicitly unmatched; missing completion provenance stays unknown.
DB fallback text remains verbatim without inferred provenance.

All reviewed nonempty text in this selected capture now has resolved attribution,
including generated notices of deliberate actions. This is asserted through JSON
export with unchanged text, native identities, ordering, and timestamps. Empty
messages have no text range to classify. Missing/contradictory model provenance,
broken or cyclic parent chains, duplicate IDs, unsupported renderings, and
ambiguous argument matches remain explicit controlled uncertainty cases.

The selected capture has image attachments, not selection attachments. IDE
selection schema support, explicit system-sourced user input, unfamiliar source
values, malformed fields, Unicode, and ambiguous matches are explicitly controlled
boundary tests. They do not establish additional native-format coverage. Native
snapshots and delivery-reference bookkeeping are not duplicated into conversation
messages. No paid CLI is invoked by these tests.
