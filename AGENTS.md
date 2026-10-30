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
uv run --locked mypy src
uv run --locked lint-imports
uv run --locked pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
uv run --locked diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
uv run --locked pytest scripts/test_extract_codex_session_log.py --cov=session_log_extractors.extract_codex_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_claude_session_log.py --cov=session_log_extractors.extract_claude_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_copilot_session_log.py --cov=session_log_extractors.extract_copilot_session_log --cov-fail-under=95
```

The locked pre-commit hooks run Ruff, package-wide mypy, and Import Linter.
Install with `uv run --locked pre-commit install`; run them with
`uv run --locked pre-commit run --all-files`. CI uses the same tool versions and
settings. Mypy enforces strict mode, `disallow_any_unimported`, and
`disallow_any_explicit` across the entire source package.
Fix type errors through accurate types, typed construction, and checked narrowing.
Casts and `no_type_check` APIs are banned by Ruff. Do not replace casts with
broader `Any` annotations, unchecked validators, or ignored errors.
Type-checker suppressions follow the same exception policy as Ruff below.

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

Import Linter enforces model/exporter dependency directions and the permitted
importers of harness schemas. Native-fixture contract tests cover source-free,
uncapped interpretation and non-mutating export for protocol adopters. Review
must still assess semantic ownership, naming, and the modeling rules below.

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

When adding shared model fields, choose names and types that distinguish
recorded data from derived conclusions. Separate values with different meanings
into separate fields. If that distinction requires a long explanation,
reconsider the representation before adding comments.

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

Keep native tool payload types and schema recognition in harness-owned modules.
Share genuinely common primitives and explicit type unions where the shared
contract needs them. Recognize tool names within their harness; matching names
alone do not establish equivalent payloads or semantics.

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

### Tests backed by generated native captures

Treat LLM-generated wording as variable fixture data. Native-capture tests must
not require particular assistant prose, question wording, option labels,
headers, free-text answers, generated shell syntax, or model version names
unless that exact value is explicitly controlled by the test. Regenerating a
capture should not require rewriting prose expectations or approving a new
output baseline merely because the model chose different words.

Assert the feature's observable contract. For ordinary generated text, check
string type and nonempty content where a response is required. For selected
answers, check single/multiple selection counts, uniqueness, and membership in
the recorded options. Keep precise checks for tool/result ownership, ordering,
complete payload retention, images, decision states, and export byte limits.
Missing required feature evidence must fail explicitly; a nonempty session
alone does not establish coverage of its individual features.

Select native evidence by tool names, structured properties, or deliberately
prepared assets and operator markers, rather than incidental generated text.
For example, find a multi-select question by its native flag and overlapping
options by the relationship between their labels, not particular label words.
Use recorded IDs to associate selected calls and results. Direct comparisons
with recorded fields or assets are appropriate when testing their preservation;
do not build a second extractor or derive expected output through the same
production logic merely to obtain an oracle for generated prose.

Keep exact parsing expectations in focused, explicitly controlled cases. For
example, supply comma-separated answers or overlapping labels deliberately and
assert their exact parsed values there, while native-capture tests check the
feature independently of those words. When changing fixture selectors or
wording-dependent assertions, verify that the tests tolerate reworded questions,
labels, corresponding answers, and assistant prose while retaining checks for
the behavior under test.

## Readability review

Review changed functions for readability even when lint passes. For functions
exceeding roughly 50 body lines, explicitly check whether they mix
responsibilities, obscure the main flow, or contain dense nested expressions.
Treat length as a review signal, not a hard limit. Extract helpers only when
they name a meaningful operation and make the caller easier to follow; do not
split code merely to reduce line counts. Long declarative structures, test
setup, and expected data can remain together when that improves understanding.
Apply the same judgment to shorter functions with dense or mixed logic.

Tests should read as specifications: make the scenario, action, and expected
outcome easy to follow. Use shared fixtures and expressive helpers to hide
incidental setup, while keeping the behavior under test and its expected result
explicit. Name helpers for the domain concept they provide or the operation
they perform. A reader should not need to trace a chain of helpers to discover
what a test does or why it passes. Prefer a little repetition over abstractions
that obscure the example.

In Python tests and test helpers, use explicit loops and named intermediate
values instead of comprehensions or generator expressions. Keep setup, actions,
and assertions separate; never hide mutations inside assertions. Collect and
name actual and expected values before comparing them. Names should explain
their meaning without requiring readers to reconstruct earlier mutations,
positional indexes, or slices. Give each helper one clear input shape.

Name classes and helpers for the responsibility they perform. Distinguish
records representing domain facts from objects that build, read, or export
those records; include lifecycle or scope when it helps explain their role.
For example, `_CopilotSessionBuilder` describes a temporary session assembler
more clearly than `NativeActivity`. Mark module-internal implementation classes
and helpers with a leading underscore; reserve public names for intended
supported interfaces. Avoid adding classes or design-pattern suffixes solely
to satisfy this naming guidance.

## Lint policy

Ruff checks application code, fixture generators, and tests. Keep the enabled
rules and initial configurable limits (complexity 10, statements 50, branches
12, arguments 5); these are Ruff defaults, not universal quality standards.
Agents must never add new lint exceptions or broaden existing ones. Do not add
suppressions, disable rules, exclude files or directories, or raise thresholds
to make a check pass. Fix the code to satisfy the rules. This also applies to
adding or broadening type-checker ignores and weakening mypy settings. PR review
must check the diff for new or broadened lint/type exceptions and weakened
configuration; these are review responsibilities, not automated guarantees. Remove existing
ignore comments when fixing or replacing the code they cover; do not add new
ones.

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
