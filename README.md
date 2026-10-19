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

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (CI uses
0.12.11), then sync the project and test tools from the committed lockfile:

```sh
uv sync --locked --extra test
```

uv manages `.venv` automatically; no activation is needed. Python 3.11 or newer
is supported; CI uses 3.11 (`uv sync --locked --extra test --python 3.11` to match).
Ruff is pinned in `pyproject.toml` and all test dependencies are locked in
`uv.lock`. Format Python with `uv run --locked ruff format .`. Run the same
checks as CI before opening a PR:

```sh
uv run --locked ruff format --check .
uv run --locked pytest --cov=scripts --cov=src --cov-report=term --cov-report=xml --cov-fail-under=95
git fetch origin main
uv run --locked diff-cover coverage.xml --compare-branch=origin/main --fail-under=95
uv run --locked pytest scripts/test_extract_codex_session_log.py --cov=extract_codex_session_log --cov-fail-under=95
uv run --locked pytest scripts/test_extract_claude_session_log.py --cov=extract_claude_session_log --cov-fail-under=95
```

Overall coverage excludes test modules. Overall, changed-line, Codex, and Claude
coverage must each reach 95%. Run diff-cover immediately after the overall test
run, which generates `coverage.xml`.

After intentionally changing dependencies in `pyproject.toml`, run `uv lock`
and commit `uv.lock` with the change. Use `uv lock --upgrade` only when intending
to upgrade the locked dependencies.
