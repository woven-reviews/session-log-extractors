# Claude Code fixture provenance

## Current test capture

Tests explicitly select **2.1.312**, captured interactively on 2026-10-30.
The process auto-updated to 2.1.314 for exact-session resumes; both versions
contributed to this one complete session.

### Capture provenance

The source run is `20261030T143456Z`. Detailed coverage, launch records, file
hashes, and sanitization reports belong to the untracked evidence bundle at
`captures/claude/2.1.312/20261030T143456Z/`; they are not fixture inputs and must
not be promoted with future captures.

Sanitization used the existing `sanitize_capture` public API at revision
`e10eb43c202030db1af05408ac94c3e39387a833` with explicit private rules retained
outside the repository. A narrow text-mode second pass redacted home prefixes
in file-history JSON keys. Repeated sanitization produced identical outputs
and match counts; native image payloads were preserved.

The required child transcript and metadata are in the main session's
[`subagents/` directory](2.1.312/native/65036844-09ed-4e91-8ed7-8636dfd163db/subagents/).
Required source images, before/after notebook inputs, plan-editor assets, and
skill/command definitions are retained under [`assets/`](2.1.312/assets/).
The selected main session is the successful retry; the failed initial attempt
had no durable native ID and was not merged into it.

### Test inputs

The canonical input is
[`native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl`](2.1.312/native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl).
Test modules pin this version and discover its main transcript and child file
without embedding captured session or agent IDs. Integration and source-free
contract tests read the complete transcript; focused tests select unchanged
records by tool name and input, operator-authored markers, or question structure.
Missing or ambiguous semantic matches fail explicitly. Recorded IDs connect
already selected calls with their results; no lookup uses a saved ID, line
number, timestamp, or occurrence. Filesystem tests copy this input
and its child sidecar into temporary directories. No tests invoke paid CLIs.

Generated assistant responses and subagent prose are checked for string type and
nonempty content, without requiring particular words or a successful recall by
the model. Question selectors use multi-select structure or the overlapping
option labels under test, rather than generated question wording or headers.
Native answers are checked for nonempty text, single or multiple selections,
and membership in recorded options, without pinning option labels or headers.
Comma-separated answers and overlapping labels have separate, explicitly
controlled parser tests. Plan selectors use recorded approval/rejection evidence
rather than proposed plan wording. Exact assertions remain for structured
decisions, operator input, edited-plan assets, and focused parsing or sanitization
boundaries. Selection
tests vary generated prose, question wording, option labels/descriptions and
their recorded answers, IDs, and record order to check that new captures can use
different wording.

The current capture supplies ordinary Markdown/Unicode, single-choice and
multi-question/multi-select answers, free text, previews and notes, question
cancellation, plan rejection/revision/editor approval, permissions, file and
notebook edits, large inputs and multibyte results, shell success/errors,
attachments, repeated skills, command arguments, subagent activity, model changes,
exact resume, and compaction. Failed capture attempts remain documented in the
untracked evidence bundle; they must not be treated as successful evidence.

Claude converted all four original image formats to PNG in the native transcript.
Tests assert that current behavior, plus legacy MIME preservation by substituting
the captured JPEG/GIF/WebP source assets into native image blocks. Plan tests
compare proposed and edited versions with the captured editor assets.

## Authorship evidence

Production authorship tests use this interactive capture for typed and queued
human input (`promptSource`, `turnOrigin`, and `origin.kind`), injected
`isMeta` expansions, task-notification origins, and compact summaries. The
operator's typed literal XML demonstrates that wrapper spelling cannot establish
injection. The five captured standalone interruption notices are classified as
candidate activity to retain the user's interruption action in candidate-visible
logs, even though Claude generates their wording. Exact native control-message
shape recognition excludes incidental quotes and respects explicit injection
provenance. The standalone `<bash-input>` record retains the user-submitted
shell command as candidate activity, including its wrapper. Its output remains
a separate record and inherits candidate attribution through the recorded
`parentUuid` link to a unique candidate shell-input message. Intervening messages
and record order do not establish ownership. Missing or ambiguous parents and
explicit injection provenance prevent inheritance. Quoted or embedded shell-input
examples are not recognized as native shell-mode actions. The custom
slash-command invocation has explicit human origin and human turn; its command
and arguments retain candidate attribution without requiring a typed-input flag.
This classification does not depend on the fixture command's name, and its
loaded instructions retain their separate provenance. The two native `/model`
selector invocations also count as candidate activity using their standalone
control-record shape. Their result notices retain the selected model as candidate
activity through recorded parent-ID links to unique candidate model invocations.
Model and shell result formats must match the linked input; an unrelated human
message or another command kind cannot establish attribution. Notice wording
and model names do not drive recognition. Results remain separate records.
Both native CLI representations of the manual `/compact` invocation (plain and
wrapped) retain candidate attribution. Their CLI entrypoint is preserved as
evidence; an unmarked SDK prompt containing `/compact` remains unknown. The
generated compaction summary retains harness attribution. The separate completion
notice inherits candidate attribution through its parent ID pointing to a unique
candidate compaction invocation. All previously unresolved activity records in
this capture now have evidence-backed classifications; a full-capture regression
test detects new unresolved activity without relabeling the generated summary.

A `queued_command` attachment records the original human prompt,
`source_uuid`, `delivery_id`, its own timestamp, and the longer rendered text;
the extractor retains that attachment at its recorded position with the recorded
rendered role and mixed candidate/harness ranges. It does not assign it to a
nearby human message or tool call.

There is no peer-origin delivery or explicit IDE-origin text in this capture.
The Agent child and its task notification are distinct from peer delivery.
Peer-origin/body shapes observed in separate local main-session logs are covered
only by clearly labeled controlled mutations; they are not promoted as native
peer coverage. Unknown SDK/external input and conflicting or incomplete source
fields never become candidate speech. No private conversation text is added.

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
