# Claude Code fixture provenance

## Current test capture

Tests explicitly select **2.1.312**, captured interactively on 2026-10-30.
The process auto-updated to 2.1.314 for exact-session resumes; both versions
contributed to this one complete session. See [manifest.json](2.1.312/manifest.json)
for launches, sanitization, asset hashes, and the native child sidecar, and
[coverage.json](2.1.312/coverage.json) for recorded behavior references.

The canonical input is
[`native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl`](2.1.312/native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl).
Test modules pin this version and its native paths directly. Integration and
source-free contract tests read the complete transcript; focused tests select
unchanged records by the native line numbers documented in `coverage.json`.
Selections retain native tool IDs and results. Filesystem tests copy this input
and its child sidecar into temporary directories. No tests invoke paid CLIs.

The current capture supplies ordinary Markdown/Unicode, single-choice and
multi-question/multi-select answers, free text, previews and notes, question
cancellation, plan rejection/revision/editor approval, permissions, file and
notebook edits, large inputs and multibyte results, shell success/errors,
attachments, repeated skills, command arguments, subagent activity, model changes,
exact resume, and compaction. The coverage ledger also records failed capture
attempts; these must not be treated as successful evidence.

Claude converted all four original image formats to PNG in the native transcript.
Tests assert that current behavior, plus legacy MIME preservation by substituting
the captured JPEG/GIF/WebP source assets into native image blocks. Plan tests
compare proposed and edited versions with the captured editor assets.

## Boundary and legacy compatibility tests

No test reads the older unversioned fixtures. Small, documented mutations of the
current input exercise missing/empty/null results, repeated or unmatched IDs,
unknown tools/fields, malformed options, corrupt trailing JSONL, ordering ties,
missing metadata, and large aggregate output. Handwritten inputs remain for
malformed containers, filesystem errors, and focused parser primitives where
native data cannot express the edge.

Legacy wording remains covered without loading old captures: the Claude 2.1.292
settings denial without a command, inline user feedback, the audited rejection
reason wording, and the unchanged-plan echo under an edited-plan heading.
These tests retain the prior semantic assertions with targeted native mutations
or the exact parser input. Older image media types remain explicit legacy cases.

The print-mode generator tests also use the current native input and child
sidecar as fake CLI output. They adapt only command/response markers required by
the generator's fixed prompts and compare complete sanitized records. The generator
itself retains its existing manual workflow; it does not regenerate the interactive
capture.

## Historical inputs

The unversioned files came from Claude Code 2.1.292 on 2026-10-09, except the
hand-authored `plan_modes.jsonl`, `questions.jsonl`, `images.jsonl`, and
`truncated.jsonl`. They remain historical artifacts, outside the active test
inputs. `scripts/generate_claude_fixtures.py` manually regenerates the old print-mode
cases and makes paid calls using the operator's login. It must never run in tests.
