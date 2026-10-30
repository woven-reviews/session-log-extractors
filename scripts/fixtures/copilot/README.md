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
and separately accessible child summaries. The current JSON schema omits those
semantic fields intentionally; it exports complete tool inputs/results/errors,
ordered images and user-shell rendered text. Native user-shell streams are
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
