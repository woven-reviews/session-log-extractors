# JSON extraction implementation plan

Refactor the existing extractors into the [target architecture](architecture.md),
preserving the [extraction information contract](../scripts/fixtures/README.md).
Use small, human-reviewable changes. For harness-specific work, proceed
Claude → Codex → Copilot. This records the agreed intent and migration order;
individual tickets provide the detailed breakdown.

0. **Establish code-quality guardrails.**
   Add Ruff linting and formatting checks to configuration and CI alongside the
   existing coverage gates. Enable correctness, common-bug, import, and complexity
   checks. Start with Ruff's default limits: cyclomatic complexity **10**,
   statements per function **50**, branches **12**, and arguments **5**. These are
   configurable starting points, not universal quality standards. Inventory
   existing violations and document narrow temporary exceptions, removing them
   as the affected code is refactored. Require justification in review for
   suppressions or threshold changes. Keep mechanical formatting separate from
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

Throughout, preserve meaningful test assertions as code moves, retire
presentation-only tests with their code, and add targeted tests for changed
behavior or exposed gaps. Run configured lint checks and both **95% coverage
gates** before committing: `pytest --cov --cov-fail-under=95 --cov-report=xml`
and `diff-cover coverage.xml --compare-branch=origin/main --fail-under=95`, after
fetching `origin/main`. Never run paid fixture
generators in tests. Extract helpers around meaningful responsibilities rather
than arbitrary fragments chosen only to satisfy complexity limits.

Completion means all three harnesses produce the contracted JSON through their
adapters and the shared exporter, with the package structure in place, guardrails
enforced, and no Markdown presentation dependency.

Comprehensive native-fixture capture, improved capture coverage, human-approved
whole-output baselines, and fresh-capture drift checks remain deferred. Existing
captures are weak evidence; use the current tests as regression protection without
claiming complete native-format coverage. Frontend work belongs to the consuming
application and is outside this refactor.
