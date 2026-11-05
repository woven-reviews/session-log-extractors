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

## Capture workflow and retired inputs

Future captures use the repository's
[capture-native-session skill](../../../.agents/skills/capture-native-session/SKILL.md)
for interactive native capture, followed by sanitization and reviewed promotion.
The shared sanitizer's tests in `scripts/test_capture_sanitization.py` cover
native record and payload preservation, deterministic explicit replacements,
image preservation, and malformed input.

The obsolete print-mode generator, its dedicated fake-CLI support tests, and
15 unused unversioned artifacts were retired in QUAL-2547. Those artifacts came
from Claude Code 2.1.292 on 2026-10-09, except the hand-authored plan, question,
image, and truncated-input cases. Active boundary tests described above retain
legacy compatibility coverage using the current capture or controlled inputs.
The complete 2.1.312 capture and its required sidecars/assets remain the test
inputs; the retired workflow did not regenerate this interactive capture.

## Structured action facts (QUAL-2529)

Production interpretation attaches shared file actions to Read, Write, Edit,
NotebookEdit, MultiEdit, and Create calls; tasks to Agent/legacy Task calls; and
invocations to Skill calls and recorded command messages. Internal session lists
index those same records. Consumers receive the attached facts through JSON,
including with a zero-byte raw-result cap. Existing inputs, results, images,
message order, and legacy `files_changed` remain available under their existing
export policy. Legacy file lists include attempts and never confirm a change.

Modification confirmation requires an ID-associated structured result naming the
same path: changed patch lines, a native creation record with recorded content,
or distinct complete notebook before/after contents. Reads, generic success
flags, free-form success text, errors, missing results, mismatched paths, and
no-op patches cannot confirm modifications. Overlapping pending calls sharing
an ID leave their results unassociated. Reusing an ID after its prior result
retains the existing completed-call associations.

The 2.1.312 capture supplies Read/Write/Edit/NotebookEdit, full Agent inputs and
its recorded agent identity, repeated Skill calls, `sourceToolUseID`-linked skill
expansions, and a `parentUuid`-linked command expansion. Definitions enrich only
a unique invocation using these relationships and the native `isMeta` and
`turnCompanion` expansion markers. They never load external files. Command
wrappers retain their exact slash text and arguments, including empty values;
the plain CLI `/compact` form uses the existing native control recognition.
Standalone links, definitions, and quoted command examples remain source text
without creating invocations. Structured facts do not assert authorship.

`scripts/test_claude_actions.py` documents controlled cases for MultiEdit/Create,
no-op and incomplete results, missing/empty paths and task fields, overlapping
call IDs, repeated command wrappers, whitespace, recorded skill links, and
ambiguous/unlinked definitions. These cases are boundary specifications, not
claims that those native variants have been captured. QUAL-2786 owns additional
native capture coverage. Tests compare generated prose with its recorded source,
rather than prescribing model wording, and verify export preservation and
non-mutation with zero, short, and unlimited result caps.
