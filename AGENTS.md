# Agent instructions

Use Python 3.11+ and install development tools with `pip install '.[test]'`
inside a virtual environment. Use the pinned Ruff version from that extra.

Format Python with `ruff format .`. Before committing, run the CI checks:

```sh
ruff format --check .
pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
pytest scripts/test_extract_codex_session_log.py --cov=extract_codex_session_log --cov-fail-under=95
pytest scripts/test_extract_claude_session_log.py --cov=extract_claude_session_log --cov-fail-under=95
```

Keep all four coverage thresholds at 95% or higher. Overall coverage excludes
test modules; run diff-cover after the overall run produces `coverage.xml`.
Keep mechanical formatting separate from behavioral changes. Do not run paid
fixture-generator CLIs in tests.

The fixture generators (`scripts/generate_*_fixtures.py`) make paid CLI calls.
Their tests replace the CLI with a fake, so their code is still covered.
Use diff-cover to identify missing coverage; add tests rather than lowering
the threshold.
