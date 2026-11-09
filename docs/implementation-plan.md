# Historical JSON extraction implementation plan

This document records the original migration rationale and intended sequence,
not current implementation status or contributor policy. See the
[current architecture](architecture.md) and [AGENTS.md](../AGENTS.md) for those.
The historical steps below are retained as intent, not instructions to reapply.

Refactor the existing extractors into the shared architecture,
preserving the [extraction information contract](../scripts/fixtures/README.md).
Use small, human-reviewable changes. For harness-specific work, proceed
Claude → Codex → Copilot. This records the agreed intent and migration order;
individual tickets provide the detailed breakdown.

0. **Establish code-quality guardrails.**
   Add Ruff linting and formatting checks to configuration and CI alongside the
   existing coverage gates. Enable correctness, common-bug, import, and complexity
   checks. Start with Ruff's default limits: cyclomatic complexity **10**,
   statements per function **50**, branches **12**, and arguments **5**. These are
   configurable starting points, not universal quality standards. The original
   plan allowed
   temporary exceptions with review justification. That allowance is superseded:
   current AGENTS.md prohibits suppressions, broadened exceptions, and weakened
   lint/type settings or thresholds. Keep mechanical formatting separate from
   behavior changes.

1. **Remove Markdown output and presentation code.**
   Make JSON the sole CLI output. Preserve useful interpretation currently tied
   to Markdown—questions and answers, plans, denials, actions, and invocations—and
   its behavioral tests. Remove renderers, image-description markers, display
   formatting, and external definition loading once their consumers are gone.
   Markdown inside recorded message text remains content. Update CLI help,
   repository extraction skills, documentation, and Docker as needed. Preserve
   existing entrypoints and session-selection behavior.

3. **Relocate the surviving runtime code into the package.**
   Move it into `src/session_log_extractors/`, leaving the existing scripts as
   thin CLI entrypoints. Update imports, tests, path defaults, and Docker together.
   Keep relocation mechanical and separately reviewable. Remove presentation
   first so it does not need to be moved; establish the intended package location
   before substantial restructuring.

4. **Consolidate extraction into the agreed architecture.**
   Each harness adapter reads native sources and produces the shared session
   model; the shared exporter produces JSON. Separate source I/O from
   interpretation. Consolidate Claude and Codex's duplicate extraction paths and
   remove Copilot's dependence on formatted bullets and presentation mutations.
   Preserve all existing JSON fields and payloads while representing the
   additional required facts, authorship, ordering, images, and recorded
   associations. Reuse existing parsing logic and shared utilities; keep the
   model close to the output contract without speculative frameworks.

5. **Finish shared export policy and contract behavior.**
   Centralize truncation, byte accounting, validation, serialization, and size
   warnings. Extract structured information before truncating raw results. Set
   the default result limit to **8192 bytes**, preserving overrides, unlimited
   mode, original byte counts, and valid UTF-8. Retain the **8 MiB** warning for
   complete serialized JSON, including images. Preserve unknown or missing
   associations honestly. Produce valid JSON for empty sessions and actionable
   diagnostics for invalid input, keeping diagnostics separate from JSON. File
   and stdout output use the same serialization. Make intentional behavioral
   corrections separately identifiable and tested.

## Current verification and remaining proposals

Use the authoritative [AGENTS.md checks](../AGENTS.md) before committing:
Ruff, strict package-wide mypy, Import Linter, and all five coverage gates
(overall, changed-line, Claude, Codex, Copilot), each at least **95%**.
Do not run paid capture during tests. The scripted fixture generators and their
fake-CLI tests have been retired.

All three harnesses now use the public reader/interpreter protocol and shared
exporter. General runtime semantic validation, approved whole-output baselines,
and an automated fresh-capture drift workflow remain unfinished. Generated
wording changes must not require rewritten prose assertions or approval of a
new prose baseline; exact controlled serialization and behavioral checks remain
appropriate.

Interactive captures now cover substantial native behavior. Their evidence is
specific: Codex has no captured edited-plan editor or native IDE classification;
Copilot has no native notebook editing or question preview/annotation fields;
Claude peer-origin boundaries are controlled cases rather than evidence from
its selected capture. Consult the linked
[fixture provenance and limitations](../scripts/fixtures/README.md#native-evidence-and-limits)
for current details. Historical capture journals and sanitization history are
provenance claims, not newly revalidated observations.

Frontend presentation and consumer upload handling belong to the consuming
application. The original migration did not require general planning lifecycles,
child-conversation graphs, external definition loading, or new display models.
