# Codex fixture provenance

Tests explicitly select **one complete interactive capture for CLI 0.166.0**,
recorded on 2026-10-30 for [QUAL-2715](https://linear.app/andela/issue/QUAL-2715).
They require no login and never invoke a paid CLI. The old filtered exec/app-server
fixtures and scripted generator have been retired.

## Selected native inputs

- Main: [rollout](0.166.0/native/rollout-2026-10-30T15-49-18-01a18e5c-0900-7111-91f6-195916aa5e3c.jsonl),
  session `01a18e5c-0900-7111-91f6-195916aa5e3c`.
- Child: [complete sidecar](0.166.0/native/rollout-2026-10-30T15-53-14-01a18e5f-a3ec-7360-a6f5-c31e4e8ebb77.jsonl),
  session `01a18e5f-a3ec-7360-a6f5-c31e4e8ebb77`. Its recorded parent and history
  boundary distinguish inherited context from child activity.
- [Assets](0.166.0/assets): two marker files, multibyte text, a literal large
  write, a local skill, and distinguishable red/blue images. PNGs were generated
  locally from RGB pixels; JPEG/GIF used macOS `sips`, and WebP used Pillow in a
  disposable environment. `blue-gif-persisted.png` is decoded from the actual
  native GIF attachment block, not reconstructed from an assistant description.

The normal Codex TUI ran under tmux in a disposable workspace outside this repo.
All launches used CLI 0.166.0 with `--no-daemon --no-alt-screen`; the primary model
was explicitly selected and one short turn used another model from `/model`.
Exact-session resumes attached two PNGs, then JPEG/GIF/WebP, and restored the
original permissions after a read-only/no-approval refusal probe. A final resume
added a literal 6.7 KB request after validation found that the earlier large write
used a short expression. About 15 minutes of active operation occurred within
27 minutes elapsed, including offline validation. No CLI version change occurred.

A first attempt inherited global operator instructions, was stopped, and was
excluded from promotion. The single retry used an isolated `CODEX_HOME` with the
existing login; neither that home nor credentials are committed. Repository,
operator, extraction, and validation instructions are absent from both selected
native conversations. Original filenames, IDs, timestamps, ordering, complete
inputs/results, compaction records, and native structure are preserved.

Sanitization uses the shared `capture_sanitization.py` at base revision `10b7fba`,
with explicit private replacements for workspace/root/home/temp paths and creator
account/user IDs. JSONL normalization is followed by a JSON-safe literal text
pass for path-bearing object keys. Repeating both passes from identical raw bytes
produced identical bytes and per-rule count/hash reports. Raw originals and
private rules remain outside the repository. Sanitized terminal snapshots, action
journal, run metadata, coverage ledger, hashes, exports, and audit findings stay
untracked at `captures/codex/0.166.0/20261030T194718Z/`.

## Evidence and limits

| Behavior | Observed native evidence or limitation |
| --- | --- |
| Questions | Two-question invocation, whole-label single selections, option-plus-note, custom punctuation/Unicode, and cancellation. Multi-select/previews were unavailable; standalone notes-only input was not established. |
| Plans | Initial plan, stay-in-Plan rejection with later feedback, revised plan, ordinary implementation approval. No edited-plan editor was exposed. |
| Files/tools | Complete literal large input, 27.3 KB multibyte result, repeated reads/searches, long Unicode path, patch content/diffs, empty successful command, combined stdout/stderr, nonzero exit, and missing-file error. Code-mode JavaScript stays opaque and explicitly unsupported rather than being expanded into invented calls. |
| Permission sources | Real pending approval cancellation and later feedback; separate actual read-only settings refusal. Native cancellation is not the older CreateProcess denial shape. |
| Skills/commands | Two native skill selections/expansions; custom slash command with arguments rejected by the UI. Native notebook editing was unavailable. |
| Images | Two same-message PNG attachments, later JPEG/GIF/WebP attachments, and a separate tool-returned image. GIF converted to PNG at persistence. |
| Continuation | Queued input, stopped background terminal, permission/question cancellation, model change, exact-session resume, and manual compaction/recall. |
| Child | One actual read-only subagent, complete sidecar, recorded parent ID/history boundary, separate child interpretation/export. Explicit `agent_message` deliveries retain sender/recipient markers and become assistant contributions with other-agent authorship. There is no dedicated summary API. |
| Export | Complete recorded inputs/results checked by call IDs; budgets 0/17/4096/unlimited plus an actual split multibyte boundary; non-mutation and CLI file/stdout parity. Model-only identities, invocation/patch details, and relationships are intentional JSON omissions. |

## Post-generation test audit

Reviewed `test_extract_codex_session_log.py`, `test_codex_sources.py`,
`test_codex_tool_inputs.py`, `test_codex_capture.py`, Codex cases in
`test_capture_sanitization.py`, `test_external_definitions.py`,
`test_extractor_contract.py`, `test_session_model.py`, `test_import_contracts.py`,
and `tests/test_smoke.py`,
plus the retired generator/support tests. `codex_capture.py` contains only explicit
current-fixture paths and small native-record helpers, without version discovery.

Resolved findings: retired-fixture reads; exact generated prose/model baselines;
selection by incidental wording/position; comprehension/generator setup in Codex
cases; hidden CLI actions inside assertions; and synthetic ordinary filesystem
inputs where captured metadata/messages apply. Native records now cover complete
payloads, repeated-call ownership, image ordering, source-free interpretation,
non-mutating export, and child history. Reworded questions, headers, descriptions,
overlapping labels, answers, and assistant prose are checked separately.

Focused controlled cases remain for malformed/missing records/files, filesystem
failures, platform/discovery arrangements, byte limits, unknown tools, unmatched
or reverse-ordered results, missing/null/empty data, older bare/tagged commands,
CreateProcess/assistant denials, legacy event views, direct patch calls, and
`update_plan` variants unavailable in this capture. These are explicit compatibility
or boundary tests and do not read retired fixtures. Native fixtures are not altered
to manufacture malformed data, unavailable workflows, or successful outcomes.

Regenerate only by explicitly invoking the repository's
[capture-native-session skill](../../../.agents/skills/capture-native-session/SKILL.md)
with its [Codex guidance](../../../.agents/skills/capture-native-session/references/codex.md).
Paid capture remains bounded and outside ordinary tests and CI.

## Authorship verification

`test_codex_authorship.py` uses this complete capture and child sidecar directly.
It checks candidate text, environment/skill injection, literal markup, both image
attachment groups, candidate shell command/outcome activity, and agent deliveries.
A full-capture guard checks that all reviewed text activity has resolved attribution.
Primary-assistant responses are harness activity, and candidate image framing
and candidate-run shell outcomes remain candidate activity. Child output is
checked after the recorded history boundary, without attributing
inherited human inputs to the child. Production JSON and non-mutating export
retain the ranges, markers, timestamps, image ownership, and tool ownership.
Controlled mutations cover Unicode/mixed blocks, malformed or conflicting
provenance, legacy duplicate views, and missing evidence. No fixture text is
rewritten to make a missing native workflow appear successful.
