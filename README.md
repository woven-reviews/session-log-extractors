# session-log-extractors

Reusable extractors for Claude, Codex, and Copilot session logs.

## Extraction compatibility

The [extraction information contract](scripts/fixtures/README.md) records the
move to JSON-only output, the complete information to preserve across
harnesses, and the fixture acceptance requirements.

Claude extraction now produces only JSON. Run
`python3 scripts/extract_claude_session_log.py` to export every session in the
resolved project directory to `claude_session_log_raw_<session-id>.json` in the
project root. Use a session id or `--transcript PATH` for one session, with
`--output PATH` for an explicit JSON destination or `--output -` for stdout.
`--tool-result-bytes N` sets the tool-result cap (negative for no cap); it
replaces `--raw-tool-result-bytes`, and `--raw` and `--no-raw` are removed. Recorded Markdown, inline images, and
tool results remain in the JSON. Codex and Copilot still write their existing
artifacts pending their JSON-only migrations.

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
uv run --locked ruff check .
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

## Ruff lint policy and initial inventory

CI runs Ruff across application code, generators, and tests. E4/E7/E9 and F
catch basic Python errors; B catches common bugs; I orders imports; C4 and PIE
catch unnecessary collection/redundant patterns. PT011/PT012/PT017 strengthen
exception tests. PGH004 rejects blanket noqa comments and RUF100 rejects stale
ones. C901/PLR0915/PLR0912/PLR0913 enforce complexity/statements/branches/arguments
at 10/50/12/5. These are Ruff's configurable defaults, not universal quality
standards. Lint supplements behavioral tests; it does not prove extraction
correctness or complete native-format coverage. UP/SIM adoption is deferred.

On the pinned Ruff 0.14.0 formatting baseline (`4f5b449`), the initial inventory
was **107 findings**: 30 C901, 21 PLR0912, 13 PLR0915, 4 PLR0913, 15 B011,
10 PT017, 9 I001, 4 B905, and 1 PIE810. The 39 non-complexity findings are fixed
(imports, exception tests, explicit zip behavior, and redundant startswith). The
initial 68 complexity findings were recorded as function-level, rule-specific
exceptions. Removing Claude Markdown rendering retires six; **62 remain** below
solely because the codebase predates lint enforcement. This is temporary debt
for pre-existing code, not precedent or permission to suppress future violations.
No directory exclusion or raised threshold hides them.

The exception reasons are existing selection and asset I/O (source-boundary
tickets), presentation being removed (JSON-only tickets), interpretation being
consolidated (pipeline tickets), the existing shared export signature (model
ticket), or generator/test orchestration (dedicated cleanup ticket QUAL-2547).
Algorithms and interfaces are preserved in this tooling change.

| File in `scripts/` | Function | Rules | Removal ticket |
| --- | --- | --- | --- |
| `extract_claude_session_log.py` | `build_events` | C901, PLR0912, PLR0915 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `extract_claude_session_log.py` | `build_turns` | C901, PLR0912, PLR0915 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `extract_claude_session_log.py` | `result_note` | C901, PLR0912 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `extract_claude_session_log.py` | `select_transcript` | C901, PLR0912 | [QUAL-2514](https://linear.app/andela/issue/QUAL-2514) |
| `extract_claude_session_log.py` | `summarize_subagent` | C901, PLR0912 | [QUAL-2514](https://linear.app/andela/issue/QUAL-2514) |
| `extract_codex_session_log.py` | `_flatten_result_text` | C901 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `_image_bytes_and_ext` | C901 | [QUAL-2515](https://linear.app/andela/issue/QUAL-2515) |
| `extract_codex_session_log.py` | `_structured_answer_for_question` | C901, PLR0912 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `build_events` | C901, PLR0912, PLR0915 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `build_turns` | C901, PLR0912, PLR0915 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `image_refs_from_event` | C901, PLR0912 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `render` | C901, PLR0912, PLR0915 | [QUAL-2506](https://linear.app/andela/issue/QUAL-2506) |
| `extract_codex_session_log.py` | `render_summary` | C901, PLR0912, PLR0915 | [QUAL-2506](https://linear.app/andela/issue/QUAL-2506) |
| `extract_codex_session_log.py` | `select_transcript` | C901 | [QUAL-2515](https://linear.app/andela/issue/QUAL-2515) |
| `extract_codex_session_log.py` | `skill_refs_from_call` | C901 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_copilot_session_log.py` | `_build_turn_objects` | C901, PLR0912, PLR0915 | [QUAL-2519](https://linear.app/andela/issue/QUAL-2519) |
| `extract_copilot_session_log.py` | `_permissions_from_events` | C901 | [QUAL-2519](https://linear.app/andela/issue/QUAL-2519) |
| `extract_copilot_session_log.py` | `get_state_attachments` | C901, PLR0912 | [QUAL-2516](https://linear.app/andela/issue/QUAL-2516) |
| `extract_copilot_session_log.py` | `main` | C901, PLR0912 | [QUAL-2507](https://linear.app/andela/issue/QUAL-2507) |
| `extract_copilot_session_log.py` | `render` | C901, PLR0913, PLR0912, PLR0915 | [QUAL-2507](https://linear.app/andela/issue/QUAL-2507) |
| `generate_claude_fixtures.py` | `run_case` | C901, PLR0912, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `capture_interactive` | C901, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `clean` | C901, PLR0912 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `validate` | C901, PLR0912 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `wait_for` | C901 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_copilot_fixtures.py` | `main` | C901, PLR0912, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `session_log_envelope.py` | `build_envelope` | PLR0913 | [QUAL-2513](https://linear.app/andela/issue/QUAL-2513) |
| `skill_metadata.py` | `render_skill_lines` | C901 | [QUAL-2508](https://linear.app/andela/issue/QUAL-2508) |
| `test_generate_claude_fixtures.py` | `_fake_claude` | PLR0913 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `test_generate_codex_fixtures.py` | `_fake_cli` | PLR0913 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `test_generate_codex_fixtures.py` | `test_interactive_main_drives_requests_and_stages_native_rollouts` | C901 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |

**Agents must never add new lint exceptions or broaden existing ones.** Do not
add suppressions, disable rules, exclude files or directories, or raise thresholds
to make a check pass. Fix the code to satisfy the rules. New and replacement code
must pass without exceptions.

Every existing exception names a function, specific rules, reason, and removal
ticket. Remove it when replacing or substantially refactoring that code; do not
transfer it to replacement code. **Update this inventory in the same change**:
remove resolved exception entries, update entries for partially resolved
exceptions, and keep the remaining counts, rules, reasons, and removal tickets
accurate. Keep the initial baseline inventory clearly labeled as historical.
Unrelated fixes must not increase metrics within suppressed functions: reviewers
must compare them manually, since automated metric comparisons are deferred.
See [AGENTS.md](AGENTS.md).
