# session-log-extractors

Reusable extractors for Claude, Codex, and Copilot session logs.

## Extraction compatibility

The [extraction information contract](scripts/fixtures/README.md) records the
planned move to JSON-only output, the complete information to preserve across
harnesses, and the fixture acceptance requirements.

## Ownership

This repository is owned and maintained by Andela. The source is published
here for transparency, but **no license to use, copy, modify, or redistribute
it is granted**. All rights are reserved by Andela.

## Releases

Releases are tagged with immutable semantic versions, e.g. `v1.0.0`.

## Development

Use Python 3.11 or newer and install the test tools in a virtual environment:

```sh
python -m venv .venv
source .venv/bin/activate
pip install '.[test]'
```

Ruff is pinned in `pyproject.toml` so local formatting matches CI. Format Python
with `ruff format .`. Run the same checks as CI before opening a PR:

```sh
ruff format --check .
pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
pytest scripts/test_extract_codex_session_log.py --cov=extract_codex_session_log --cov-fail-under=95
pytest scripts/test_extract_claude_session_log.py --cov=extract_claude_session_log --cov-fail-under=95
```

Overall coverage excludes test modules. Overall, changed-line, Codex, and Claude
coverage must each reach 95%. Run diff-cover immediately after the overall test
run, which generates `coverage.xml`.
