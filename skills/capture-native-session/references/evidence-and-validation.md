# Evidence, validation, and fixture promotion

## Directory structure

Use harness, then exact observed CLI version, then capture run for evidence. Use `claude`, `codex`, and `copilot` as harness names. A UTC timestamp is a suitable run ID; it is not a substitute for native session IDs.

```text
skills/
  capture-native-session/
    SKILL.md
    references/
      claude.md
      codex.md
      copilot.md
      conversation-coverage.md
      evidence-and-validation.md
captures/                              # untracked; sanitized evidence only
  <harness>/
    <cli-version>/
      <run-id>/
        run.json
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
    <cli-version>/
      manifest.json
      coverage.json
      native/
        <original-native-filenames-and-sidecar-layout>
```

Keep raw, unsanitized evidence and private sanitization rules **outside** the repository, mirroring `<harness>/<cli-version>/<run-id>/` where convenient. Use the running process's observed launch version as the directory key; record any later version changes and per-launch versions in `run.json`. Never silently relabel an older process after an auto-update.

Ensure repository-root `captures/` is ignored before writing evidence there. Do not add committed placeholder files to it. A capture run can contain raw evidence from its one permitted retry, but keep sessions and their validation artifacts separately identified within the bundle.

For now, there is **one committed complete capture fixture per harness/version**, not a fixture directory per scenario or per evidence run. Required subagent sidecars and assets belong to that same bundle. Multiple test assertions locate their records by native IDs. Do not slice the main conversation into questions/plans/tools cases or fabricate a composite session. A retry cannot silently fill gaps in the selected main fixture.

Do not move existing fixtures, rewrite tests, or promote captures merely because this layout is specified. Promotion is a separate authorized review step. If this harness/version already has a committed capture, propose its reviewed replacement rather than adding a second fixture or overwriting it automatically.

## Evidence records

- `run.json`: harness; run ID; exact native main/retry/child identities and recorded relationships; UTC start/end and interactive duration; resolved executable and version; per-launch arguments, configuration and workspace; models that actually handled turns; resume attempts; version changes; available token/cost usage with source references. Use stable sanitized workspace placeholders. Never invent missing IDs or usage.
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
- Native identities and semantic fields omitted by the export schema. Attribute those omissions to export policy/support, not failed capture. Distinguish fields lost during interpretation from fields retained in the shared model but omitted in JSON.

If a test audit is requested, do it only after capture, outside the captured session. Compare the new native evidence with existing tests and fixtures; report capabilities the logs cannot establish. Do not manufacture malformed JSONL, missing IDs, unknown tools, out-of-order results, platform paths, or filesystem failures to expand this interactive capture. Keep those controlled boundary tests separate. Do not inflate the conversation to trigger the 8 MiB warning.

## Review and promotion

Choose the successful complete main session, retain provenance to its untracked evidence bundle, and propose copying its sanitized native records and required sidecars/assets to the single harness/version fixture directory. The committed manifest should identify the source run, exact native identities, versions, sanitization provenance, and file hashes; coverage should map requested scenarios to recorded IDs and report remaining gaps.

Keep terminal snapshots, journals, interpreted models, and budget experiments in untracked `captures/`; they are evidence rather than additional fixtures. Follow established baseline conventions if approved output baselines are requested separately. Never commit raw/private data, `captures/`, or explicitly excluded historical artifacts such as `/pilots`. Do not retire headless generators or replace existing fixture tests until separately authorized and equivalent coverage has been reviewed.
