# Evidence, validation, and fixture promotion

## Directory structure

Use harness, then exact observed CLI version, then capture run for evidence. Use `claude`, `codex`, and `copilot` as harness names. A UTC timestamp is a suitable run ID; it is not a substitute for native session IDs.

```text
.agents/
  skills/
    capture-native-session/
      SKILL.md
      references/
        claude.md
        codex.md
        copilot.md
        conversation-coverage.md
        evidence-and-validation.md
.claude/
  skills/
    capture-native-session -> ../../.agents/skills/capture-native-session
captures/                              # untracked; sanitized evidence only
  <harness>/
    <cli-version>/
      <run-id>/
        run.json
        manifest.json                 # optional audit inventory
        findings.md
        coverage.json
        actions.jsonl
        sanitization.json
        terminal/
        assets/
        native/
        interpreted/
        exports/
          result-budget-0.json
          result-budget-17.json
          result-budget-4096.json
          result-budget-unlimited.json
scripts/fixtures/                      # reviewed committed material
  <harness>/
    README.md                         # concise fixture provenance
    <cli-version>/
      assets/                         # required test inputs only
      native/
        <original-native-filenames-and-sidecar-layout>
```

Keep raw, unsanitized evidence and private sanitization rules **outside** the repository, mirroring `<harness>/<cli-version>/<run-id>/` where convenient. Use the running process's observed launch version as the directory key; record any later version changes and per-launch versions in `run.json`. Never silently relabel an older process after an auto-update.

Ensure repository-root `captures/` is ignored before writing evidence there. Do not add committed placeholder files to it. A capture run can contain raw evidence from its one permitted retry, but keep sessions and their validation artifacts separately identified within the bundle.

For now, there is **one committed complete capture fixture per harness/version**, not a fixture directory per scenario or per evidence run. Required subagent sidecars and assets belong to that same bundle. Multiple test assertions locate their records by native IDs. Do not slice the main conversation into questions/plans/tools cases or fabricate a composite session. A retry cannot silently fill gaps in the selected main fixture.

Do not move existing fixtures, rewrite tests, or promote captures merely because this layout is specified. Promotion is a separate authorized review step. If this harness/version already has a committed capture, propose its reviewed replacement rather than adding a second fixture or overwriting it automatically.

## Evidence records

- `run.json`: harness; run ID; exact native main/retry/child identities and recorded relationships; UTC start/end and interactive duration; resolved executable and version; per-launch arguments, configuration and workspace; models that actually handled turns; resume attempts; version changes; available token/cost usage with source references. Use stable sanitized workspace placeholders. Never invent missing IDs or usage.
- `manifest.json` (optional): audit inventory of selected files, hashes, and source relationships. Keep it in the untracked evidence bundle.
- `actions.jsonl`: ordered action IDs, timestamps, target session/pane, exact sanitized input or navigation, before/after snapshot paths, observed effect, and native message/tool IDs when established. Snapshot before/after significant actions, including failures. Do not associate records merely by nearby timestamps.
- `assets/`: sanitized local inputs, skill/command contents, images and provenance. Record original sizes/hashes privately and publish non-sensitive provenance. Keep sanitized text/image assets consistent with native references.
- `native/`: sanitized copies of just these sessions' transcripts, relevant sidecars and dependencies, retaining native relative layout and filenames. Retain the immutable originals outside the repo.
- `interpreted/`: complete shared `Session` representations and child summaries obtained through existing public interfaces. Describe serialization of typed models; do not invent new semantic fields to hide gaps.
- `exports/`: the four budgeted exports plus separately captured stdout/file parity results and diagnostics. Put child export artifacts in a distinct child directory if an existing interface supports them.
- `sanitization.json`: rule names and match counts, source/output hashes, format changes, and reproducibility results. Keep literal sensitive values and private rule files outside the repo.
- `coverage.json`: one entry per requested behavior, with separate stage statuses and evidence references: action/snapshot, native filename and recorded IDs, shared-model location, exported location/budget. Use `captured`, `persisted`, `interpreted`, `exported`, `failed`, `skipped`, or `not applicable` as appropriate for each stage; include a reason for gaps. A screen alone proves neither persistence nor export.
- `findings.md`: concise results, IDs/versions/duration/usage, failures/retries/skips, information loss by stage, and the smallest proposed fixture promotion step.

Use recorded IDs for associations. Retain unmatched evidence explicitly. Harness diagnostic/replay records, such as request blobs containing historical tool calls, do not establish new conversation activity.

## Deterministic sanitization

Use the repository's existing Python sanitizer, documented in the root README under “Deterministic capture sanitization,” with explicit private rules. Do not create another sanitizer or helper script. Check the current public interface before invoking it; another agent may be changing it.

1. Collect immutable raw evidence first. Never modify native records, repair a failed capture, or manufacture results/IDs.
2. Apply explicit deterministic replacements to copies. Account for personal paths, hosts, account identifiers, emails, and any credential fragments actually present, including terminal snapshots. Do not access unrelated credentials to construct a redaction list.
3. Record which rules actually matched separately from precautionary rules. Do not claim a secret existed merely because a rule was configured. Record formatting normalization separately from redaction.
4. Preserve native IDs, timestamps, ordering, structured maps, tool payloads, and image data unless an explicitly justified sensitive value requires redaction. Check binary/screenshotted assets visually; the text sanitizer does not sanitize pixels. Exclude unsafe artifacts and record the gap rather than claiming they are safe.
5. Repeat from the same raw bytes and explicit rules and compare resulting bytes/reports. Validate JSON/JSONL and native associations afterward. Re-run interpretation/exports on sanitized copies to detect semantic changes. Record sanitizer revision and hashes; keep private mapping values outside the repo.

JSON-mode sanitization leaves object keys unchanged; native file-history maps can contain personal paths as keys. Check these before delivery. If needed, use the existing sanitizer's text mode for a narrowly scoped second pass with JSON-safe literal replacements and unrelated patterns disabled. Record both passes and recheck JSON validity, image payloads, native identities, and interpretation.

## Interpretation and export checks

Use the existing extractor's public reader/interpreter/exporter interfaces. Consult interface documentation and CLI help only as needed; do not ask the captured assistant to inspect the extractor, tests, or outputs. Do not add validation scripts by default. Use existing interfaces and ordinary operator commands; if a reusable helper is genuinely necessary, explain the need before expanding scope.

Read the selected native source, including necessary sidecars/assets, then interpret once into an uncapped shared `Session`. Check source-free interpretation and non-mutating export using the established contract/testing interfaces where available. Record checks that cannot be performed rather than claiming them.

Validate against native evidence independently of parser-produced summaries:

- Native structured answer maps versus interpreted `answer_text`; selected labels, custom punctuation/Unicode, multiSelect, previews/annotations, notes-only answers, and cancellation. A successful simple answer does not establish fidelity for quoted custom text.
- Rejection feedback, revised plan, ordinary approval, and genuine edited approval separately. Preserve candidate feedback separately from harness explanations; unknown decisions are not approvals.
- Complete write/edit inputs and complete interpreted results; errors; written and silent permission denials; ownership of each repeated call/result using tool IDs. Inspect structured/non-text result blocks as well as rendered text: empty exported text can hide preserved native content.
- Tool result budgets **0, 17, 4096, and -1**, where negative means unlimited. Verify original `result_bytes`, `result_truncated`, valid UTF-8, and complete unlimited results. If these budgets miss a multibyte boundary, choose an additional budget that actually splits a recorded multibyte character. Do not assume 17 exercises that boundary.
- Export all budgets from the same shared Session and compare a deep representation before/after. Full input retention is independent of result budgets.
- CLI file output versus stdout for the exact selected transcript/session and same settings; parse JSON and compare semantically. Capture diagnostics separately so they cannot contaminate JSON. Document unavailable explicit-source selection rather than validating a different session discovered by default.
- Subagent summaries through the existing sidecar reader, separately from main-session export. Do not mistake the parent's task summary for the child's conversation.
- Repeated skill invocations by their distinct native call/result IDs, retaining both calls while checking deduplication of the session skill list. Validate human permission denials and settings-driven refusals separately against the recorded controls/configuration and result text.
- Image attachments by owning native message ID, block order, payload/asset content and persisted media type through interpretation and export. Keep source-format conversion separate from missing or altered persisted content.
- Native notebook edits against the before/after cell assets and complete recorded path/cell inputs/results. Command arguments against the exact invocation, command definition, native argument fields and expanded content; identify interpretation loss separately from absent native fields.
- Running-tool interruption and pending-approval cancellation against observed UI state and native IDs, including any error/result or unanswered call. Check subsequent continuation records and result ownership without manufacturing missing or unmatched records.
- Concurrent/batched reads and queued follow-up input against actual native message/call/result order and distinct asset markers. Preserve serialized execution or deferred input explicitly when that is what the CLI records.
- Empty-success and successful stdout/stderr results against native exit status, streams and complete blocks. Tool-returned images against the owning call and full result payload, separately from user attachments and textual export.
- User-shell input and both stdout/stderr markers against actual user-shell records, separately from assistant tool output. Literal harness-like markup in a human's fenced code block against the exact typed message, checking that noise cleanup does not remove human-authored content.
- Notes-only, selection-plus-note and overlapping-label answers against native answer maps/annotations. Long generated paths against complete tool inputs and file-change records. Repeated native local searches against retained payloads, schema recognition and deduplicated diagnostics.
- Manual compaction against the private pre/post source snapshots, session identity, persisted summary/boundary records and recall response. Record any history rewrite/loss and distinguish replayed context from newly recorded activity.
- Native identities and semantic fields omitted by the export schema. Attribute those omissions to export policy/support, not failed capture. Distinguish fields lost during interpretation from fields retained in the shared model but omitted in JSON.

If a test audit is requested, do it only after capture, outside the captured session. Compare the new native evidence with existing tests and fixtures; report capabilities the logs cannot establish. Do not manufacture malformed JSONL, missing IDs, unknown tools, out-of-order results, platform paths, or filesystem failures to expand this interactive capture. Keep those controlled boundary tests separate. Do not inflate the conversation to trigger the 8 MiB warning.

Treat additional tests using the existing transcript as reuse opportunities, not substitutes for uncaptured supported interactions. In `coverage.json`, give genuine edited-plan approval, repeated skill invocation, settings-driven denial, same-message/later-message attachments, each attempted image format, native notebook editing, command arguments, running-tool interruption, and pending-approval cancellation separate outcomes with stage-specific evidence or an observed reason for the gap. Branch coverage alone does not establish fidelity to a changing native format.

Also report independent reads, queued input, empty-success results, successful stdout/stderr, tool-returned images, notes-only and selection-plus-note submissions, overlapping labels, repeated local searches, long paths, and manual compaction/recall individually. Record pre/post-compaction source provenance in `run.json`; do not publish private originals or claim a rewritten final source contains discarded history.

Give literal human-authored markup and user-shell stdout/stderr separate coverage entries with native message IDs and stage-specific outcomes. Do not count assistant Bash streams as evidence for user-shell output retention.

## Review and promotion

Choose the successful complete main session, retain provenance to its untracked evidence bundle, and propose copying its sanitized native records and required sidecars/assets to the single harness/version fixture directory. Add concise provenance to the harness fixture README: source run, CLI versions and any version changes, main transcript and required sidecars/assets, sanitization method/revision, and material capture limitations. Keep detailed file hashes, native-ID coverage maps, failures/retries, and audit inventories in the untracked evidence bundle.

Do not commit capture `coverage.json` or audit `manifest.json` files; versioned fixture paths for these files are ignored. Preserve manifests only while tests still consume them. After an authorized migration, remove obsolete captures, manifests, assets, generator code, and fake-CLI support tests; keep only documented, test-consumed legacy or malformed boundary inputs. Before promotion, check the staged file list for audit artifacts and ensure README links point to committed inputs rather than untracked reports.

Keep terminal snapshots, journals, interpreted models, and budget experiments in untracked `captures/`; they are evidence rather than additional fixtures. Follow established baseline conventions if approved output baselines are requested separately. Never commit raw/private data, `captures/`, or explicitly excluded historical artifacts such as `/pilots`. Do not retire headless generators or replace existing fixture tests until separately authorized and equivalent coverage has been reviewed.
