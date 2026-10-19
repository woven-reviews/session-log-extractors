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
uv run --locked pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
uv run --locked diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
uv run --locked pytest scripts/test_extract_codex_session_log.py --cov=extract_codex_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_claude_session_log.py --cov=extract_claude_session_log --cov-fail-under=95
```

Keep all four coverage thresholds at 95% or higher. Overall coverage excludes
test modules; run diff-cover after the overall run produces `coverage.xml`.
Keep mechanical formatting separate from behavioral changes. Do not run paid
fixture-generator CLIs in tests.

The fixture generators (`scripts/generate_*_fixtures.py`) make paid CLI calls.
Their tests replace the CLI with a fake, so their code is still covered.
Use diff-cover to identify missing coverage; add tests rather than lowering
the threshold.

## Lint policy

Ruff checks application code, fixture generators, and tests. Keep the enabled
rules and initial configurable limits (complexity 10, statements 50, branches
12, arguments 5); these are Ruff defaults, not universal quality standards.
Do not add blanket exclusions or blanket noqa comments. Additional suppressions
or raised thresholds require explicit human approval and justification in review.
Each temporary exception must identify the function, specific rules, reason, and
removal ticket (see the inventory in README.md). Remove exceptions when replacing
or substantially refactoring the affected code; never transfer them to replacement
code. Unrelated fixes must not increase metrics inside suppressed functions;
reviewers must compare those metrics because automated comparisons are deferred.
