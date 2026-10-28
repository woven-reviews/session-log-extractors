# Agent instructions

Use uv (CI pins 0.12.11) and Python 3.11+. Install development tools with
`uv sync --locked --extra test`; add `--python 3.11` to match CI's Python version.
uv manages `.venv` without manual activation. Use the committed `uv.lock` and
the pinned Ruff version from the test extra. After intentional dependency
changes, run `uv lock` and commit the updated lockfile.

Format Python with `uv run --locked ruff format .`. Before committing, run the
CI checks:

```sh
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy src scripts/check_type_escapes.py
uv run --locked pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
uv run --locked python scripts/check_type_escapes.py --compare-branch=origin/main
uv run --locked diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
uv run --locked pytest scripts/test_extract_codex_session_log.py --cov=session_log_extractors.extract_codex_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_claude_session_log.py --cov=session_log_extractors.extract_claude_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_copilot_session_log.py --cov=session_log_extractors.extract_copilot_session_log --cov-fail-under=95
```

Run mypy across the entire source package, including bodies of untyped functions.
Fix type errors through accurate types, typed construction, and checked narrowing.
Casts and `no_type_check` APIs are banned by Ruff. Do not replace casts with
broader `Any` annotations, unchecked validators, or ignored errors.
The type exception inventory (`scripts/typecheck_exceptions.json`) is currently
empty. CI compares it with origin/main and rejects new or broadened exceptions,
stale entries, policy-disabling comments/configuration, and unapproved ignores.
The inventory may only shrink; never add ignores or weaken enforcement to pass
checks. Existing exceptions, if any, must name exact error codes, the file,
enclosing symbol, statement, reason, and removal ticket.

Keep all five coverage thresholds at 95% or higher. Overall coverage excludes
test modules; run diff-cover after the overall run produces `coverage.xml`.
Keep mechanical formatting separate from behavioral changes. Do not run paid
fixture-generator CLIs in tests.

When extracting code into a new module, move its existing unit and boundary
tests into the corresponding test module in the same change; keep integration
tests with the behavior they exercise.

The fixture generators (`scripts/generate_*_fixtures.py`) make paid CLI calls.
Their tests replace the CLI with a fake, so their code is still covered.
Use diff-cover to identify missing coverage; add tests rather than lowering
the threshold.

## Shared model and export boundaries

Use `extractor_contract.Extractor[Request, Source]` for public reader/interpreter
stages. Implementations must satisfy the protocol under mypy and be exercised
by the native-fixture contract tests. Keep source access in `read_source` and
return an uncapped shared `Session` from `interpret_source`. Extend the shared
contract test matrix when another harness adopts it; do not claim conformance
through a wrapper that still applies export policy during interpretation.

Preserve meaningful domain information in named, typed fields on the shared
model, with named record types for structured concepts. Do not introduce generic
`metadata`, `extra`, or `interpretation` dictionaries for information whose
meaning is already known. Passing regression tests alone does not establish
that a model or abstraction is appropriate; review where each semantic concept
belongs and whether the model duplicates another representation.

Use ordered, typed messages and their tool calls as the canonical conversation.
Associate results, questions, and decisions using recorded message/tool IDs
where available. Do not infer ownership from the most recent human message or
timestamp proximity. If a consumer needs chronological interaction groups,
derive that view from canonical records. Preserve unmatched records explicitly
rather than inventing an association.

Define specific input types for known tools and use a recursive JSON value type
for opaque payloads. Types describe the fields we understand; they must not
filter out additional recorded fields. Preserve unsupported tool payloads and
report the missing schema support explicitly instead of silently treating them
as fully understood. Schema recognition is distinct from runtime validation.

Treat output compatibility as a serialization constraint. Exporters should read
the fields required by their output schema, leaving other model fields available
to consumers; do not serialize the whole model and then delete semantic fields.
Keep complete inputs/results until export applies the configured output policy.

## Test data policy

Prefer existing committed fixtures for new or substantially changed extractor
tests, especially captured native session logs. Inspect the relevant fixture
files and their provenance before constructing transcript entries by hand.
Load fixtures directly for interpretation tests; when testing filesystem
behavior, copy them into temporary directories. For tests that prohibit
filesystem access during interpretation, load fixtures before enabling the
filesystem guard.

Use small, targeted changes to fixture data when a test needs a specific
boundary condition, and explain those changes in the test. Preserve the
behavioral assertions when replacing synthetic inputs with fixtures.
Hand-authored inputs remain appropriate for malformed data, missing files,
filesystem errors, and other cases that existing fixtures cannot meaningfully
exercise. Do not replace a focused edge-case test with a fixture that no longer
tests that edge case, or invoke paid fixture generators as part of tests.

## Lint policy

Ruff checks application code, fixture generators, and tests. Keep the enabled
rules and initial configurable limits (complexity 10, statements 50, branches
12, arguments 5); these are Ruff defaults, not universal quality standards.
Agents must never add new lint exceptions or broaden existing ones. Do not add
suppressions, disable rules, exclude files or directories, or raise thresholds
to make a check pass. Fix the code to satisfy the rules.

The exceptions inventoried in README.md exist solely because this codebase
predates lint enforcement. They are temporary debt for pre-existing code, not
precedent or permission to suppress violations in new or replacement code.
Each identifies its function, specific rules, reason, and removal ticket.
Remove the exception when replacing or substantially refactoring that code;
never transfer it to replacement code. Update README.md in the same change:
remove resolved exception entries, update entries for partially resolved
exceptions, and keep the remaining counts, rules, reasons, and removal tickets
accurate. Keep the initial baseline inventory clearly labeled as historical.
Unrelated fixes must not increase metrics inside suppressed functions; reviewers
must compare those metrics because automated comparisons are deferred.
