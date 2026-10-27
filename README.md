# session-log-extractors

Reusable extractors for Claude, Codex, and Copilot session logs.

## Extraction compatibility

The [extraction information contract](scripts/fixtures/README.md) records the
move to JSON-only output, the complete information to preserve across
harnesses, and the fixture acceptance requirements.

Claude extraction produces JSON. Run
`uv run --locked python scripts/extract_claude_session_log.py` to export every session in the
resolved project directory to `claude_session_log_raw_<session-id>.json` in the
project root. Use a session id or `--transcript PATH` for one session, with
`--output PATH` for an explicit JSON destination or `--output -` for stdout.
`--tool-result-bytes N` sets the tool-result cap (negative for no cap).
Recorded Markdown, inline images, and tool results remain in the JSON.

Codex extraction also produces only JSON. Run
`uv run --locked python scripts/extract_codex_session_log.py` to export every matching project
session to `codex_session_log_raw_<session-id>.json` in the project root.
Session selection, `--output PATH`, `--output -`, and `--tool-result-bytes N`
work as described above; `--sessions-root DIR` overrides the session root and
`--strict` requires an exact recorded working directory match. Codex also
removes `--raw`, `--no-raw`, and `--raw-tool-result-bytes`. Upload the JSON files
with inline images; no generated Markdown or image description pass is needed.
Recorded Markdown, inline images, and tool results remain in the JSON.

Copilot extraction produces JSON. Run
`uv run --locked python scripts/extract_copilot_session_log.py` to export every matching session
to `copilot_session_log_raw_<identifier>.json` in the project root. Use a session
id or unique prefix to select one session, `--output PATH` for its JSON file,
or `--output -` for the selected or newest session on stdout. With `--all`,
`--output DIR` selects the destination directory. `--strict` requires an exact
working-directory match during discovery; `--db PATH` selects a database.
`--tool-result-bytes N` sets the tool-result cap (negative for no cap).
Tool inputs and results remain structured, and available images are inlined as
base64; unavailable image references remain explicit in the JSON.

Extraction uses recorded skill/command invocations and payloads without reading
local definitions. Recorded Markdown remains message content; the extractors do
not generate Markdown or image descriptions.

## Running the installed package

Run local commands above from the repository root after `uv sync --locked`.
uv installs the shared exporter from `src/session_log_extractors/`; keep the
existing extractor scripts as entrypoints.

Build Docker from the repository root so the context includes `pyproject.toml`
and `src/`:

```sh
docker build -f scripts/Dockerfile -t session-log-extractor .
docker run --rm session-log-extractor -c 'from session_log_extractors import session_log_envelope'
```

The repository extraction helpers mount the verified checkout and invoke its
`scripts/extract_*_session_log.py` with the image's Python and installed package.
The Claude and Copilot scripts delegate to their `session_log_extractors` modules
and supply the checkout root for default output paths, including from subdirectories.
Copilot retains `~/.copilot/session-store.db` and `~/.copilot/session-state`
for discovery; `--db PATH` still overrides the database location.
Repository matching uses the current working directory; default output uses the checkout root.
Direct calls to the Claude and Copilot package modules' `main()` use the working
directory at call time for default output, or an explicit `project_root=Path(...)`.
Run `uv run --locked pytest tests/test_smoke.py` to check package imports and
fixture exports through all three local script entrypoints.

Claude readers live in `session_log_extractors.claude_sources`. `read_source(path)`
loads main records; `read_subagent_sources(path)` reads the sibling sidecars with
optional metadata and retains each source path. For replay without filesystem
access, construct `ClaudeSource(path, entries, metadata)` directly and call
`extract_claude_session_log.interpret_source(source)` or
`summarize_subagent_source(source)`. Images remain inline in the recorded entries.
Main JSON extraction continues to omit sidecars; `load_subagents(path)` retains
its timestamp-ordered summaries for existing callers.

## Shared session/export boundary

Adapters can supply `Session`, `Event`, `ToolCall`, and `Image` dictionaries from
`session_log_extractors.session_model` directly to
`session_log_extractors.session_log_envelope.export_session(session, tool_result_max_bytes=4096)`.
Supply complete interpreted content: export copies it before applying the existing
UTF-8 byte cap to textual tool results, retaining original byte counts and
truncation flags. Negative limits retain full text. Structured results, nested
payloads, images, ordering, and missing/null/empty values pass through unchanged.
The model has no presentation state, adapter base class, or runtime validation.

Existing harnesses continue applying their own limits and use
`build_envelope(Session(...), tool_result_max_bytes=...)` only to assemble the
legacy envelope. Their interpretation migrations and shared policy rollout are
separate follow-ups; current CLI behavior and defaults are unchanged.

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
uv run --locked pytest scripts/test_extract_claude_session_log.py --cov=session_log_extractors.extract_claude_session_log --cov-fail-under=95
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
exceptions.
solely because the codebase predates lint enforcement. This is temporary debt
for pre-existing code, not precedent or permission to suppress future violations.
No directory exclusion or raised threshold hides them.

The exception reasons are existing selection and asset I/O (source-boundary
tickets), presentation being removed (JSON-only tickets), interpretation being
consolidated (pipeline tickets), or generator/test orchestration (dedicated
cleanup ticket QUAL-2547).
Algorithms and interfaces are preserved in this tooling change.

| File (relative to `scripts/` unless shown) | Function | Rules | Removal ticket |
| --- | --- | --- | --- |
| `src/session_log_extractors/extract_claude_session_log.py` | `build_events` | C901, PLR0912, PLR0915 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `src/session_log_extractors/extract_claude_session_log.py` | `build_turns` | C901, PLR0912, PLR0915 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `src/session_log_extractors/extract_claude_session_log.py` | `result_note` | C901, PLR0912 | [QUAL-2517](https://linear.app/andela/issue/QUAL-2517) |
| `extract_codex_session_log.py` | `_flatten_result_text` | C901 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `_image_bytes_and_ext` | C901 | [QUAL-2515](https://linear.app/andela/issue/QUAL-2515) |
| `extract_codex_session_log.py` | `_structured_answer_for_question` | C901, PLR0912 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `build_events` | C901, PLR0912, PLR0915 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `build_turns` | C901, PLR0912, PLR0915 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `image_refs_from_event` | C901, PLR0912 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `extract_codex_session_log.py` | `select_transcript` | C901 | [QUAL-2515](https://linear.app/andela/issue/QUAL-2515) |
| `extract_codex_session_log.py` | `skill_refs_from_call` | C901 | [QUAL-2518](https://linear.app/andela/issue/QUAL-2518) |
| `src/session_log_extractors/extract_copilot_session_log.py` | `_build_turn_objects` | C901, PLR0912, PLR0915 | [QUAL-2519](https://linear.app/andela/issue/QUAL-2519) |
| `src/session_log_extractors/extract_copilot_session_log.py` | `_permissions_from_events` | C901 | [QUAL-2519](https://linear.app/andela/issue/QUAL-2519) |
| `src/session_log_extractors/extract_copilot_session_log.py` | `get_state_attachments` | C901, PLR0912 | [QUAL-2516](https://linear.app/andela/issue/QUAL-2516) |
| `generate_claude_fixtures.py` | `run_case` | C901, PLR0912, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `capture_interactive` | C901, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `clean` | C901, PLR0912 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `validate` | C901, PLR0912 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_codex_fixtures.py` | `wait_for` | C901 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
| `generate_copilot_fixtures.py` | `main` | C901, PLR0912, PLR0915 | [QUAL-2547](https://linear.app/andela/issue/QUAL-2547) |
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
