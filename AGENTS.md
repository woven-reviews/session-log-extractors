# Agent instructions

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
```

## Tests and coverage

CI enforces two 95% gates. Run both before committing:

```bash
.venv/bin/pytest --cov --cov-fail-under=95 --cov-report=xml
.venv/bin/diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
```

- The first gate covers all of `scripts/` and `src/`, test files excluded.
- The second gate covers only the lines this branch changes, including uncommitted edits. Run `git fetch origin main` first so `origin/main` is current.
- `diff-cover` lists the changed lines that are missing coverage. Add tests for them; don't lower the threshold.

The fixture generators (`scripts/generate_*_fixtures.py`) make paid CLI calls. Never run them in tests. Their tests replace the CLI with a fake, so their code is still covered.
