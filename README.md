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

Codex filesystem discovery, selection, transcript loading, and optional image
asset reads live in `session_log_extractors.codex_sources`. The extractor's
`read_source(path)` loads a `CodexSource` with records, bounded session metadata,
and image inputs. `interpret_source(source)` returns an uncapped shared `Session`
without filesystem access; `build_raw_envelope(source)` exports it. Missing
path inputs retain unavailable-image markers. Export reuses loaded metadata;
discovery retains the original 50-line ID and 80-line cwd lookup windows.

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
The Claude, Codex, and Copilot scripts delegate to their `session_log_extractors` modules
and supply the checkout root for default output paths, including from subdirectories.
Copilot retains `~/.copilot/session-store.db` and `~/.copilot/session-state`
for discovery; `--db PATH` still overrides the database location.
Repository matching uses the current working directory; default output uses the checkout root.
Direct calls to the package modules' `main()` use the working
directory at call time for default output, or an explicit `project_root=Path(...)`.
Run `uv run --locked pytest tests/test_smoke.py` to check package imports and
fixture exports through all three local script entrypoints.

Claude readers live in `session_log_extractors.claude_sources`. `read_source(path)`
loads main records; `read_subagent_sources(path)` reads the sibling sidecars with
optional metadata and retains each source path. For replay without filesystem
access, construct `ClaudeSource(path, entries, metadata)` directly and call
`extract_claude_session_log.interpret_source(source)` or
`summarize_subagent_source(source)`. Images remain inline in the recorded entries.
Claude interprets main records once into the shared session and exports with
`export_session`; byte limits apply only at export. `interpret_source` accepts
no result cap. `Session.events` holds ordered `UserMessage` and
`AssistantMessage` records. User messages retain recognized commands and shell
actions; tool calls retain native IDs, results, questions, plan decisions,
permission denials, and skill invocations. Results and decisions attach to their
call by native ID even across later human messages. Native message/parent IDs
and complete result records remain available on the model. Known tool inputs
have named schemas in `tool_inputs/claude.py`, including `BashInput` and
`ReadInput`. Copilot owns its payload types and tool-name recognition in
`tool_inputs/copilot.py`, with explicit `CopilotBash`, `CopilotSkill`, and file-tool
schema identifiers. Each extractor recognizes only its own tool vocabulary.
`tool_inputs/__init__.py` combines the harness types for the shared model without
a global registry. Opaque values use `json_types.JsonValue`; the question-option
shape used by the shared model lives in `question_types.py`. Schema recognition preserves additional keys and
does not validate payloads at runtime. `ToolCall.input_schema` and
`Session.unsupported_tools` identify unsupported names or non-object inputs;
the CLI reports them on stderr while preserving their payloads. Unmatched results
are retained explicitly instead of assigned by proximity. `Session.files_changed`,
`skills_used`, and `subagent_count` hold session facts without turn grouping.
The shared exporter selects current JSON fields from messages and calls while
preserving complete nested input/result payloads. `build_events` remains an
export convenience wrapper. `Turn`, `Interaction`, and chronological sidecar
attribution have been removed. Main JSON extraction omits sidecars;
`load_subagents(path)` retains its timestamp-ordered summaries for existing callers.

Copilot readers and discovery live in `session_log_extractors.copilot_sources`.
`read_source(connection, session)` loads metadata, required database rows, models,
and ordered state records into `CopilotSource`. The extractor's `read_source`
also snapshots referenced external image bytes, following the Codex reader pattern.
`EXTRACTOR: Extractor[CopilotRequest, CopilotSource]` exposes the production stages.
`CopilotRequest` holds the selected metadata and caller-owned connection.
`interpret_source(source)` returns an uncapped shared `Session` without database
or filesystem access; `build_raw_envelope(source)` applies `export_session`.
Ordered native messages own their tool requests and images; results join by
recorded call ID. Repeated and orphan results remain in `unmatched_tool_results`,
and the complete native result stays in `result_record` beside the compatible
textual JSON result. Database-only inputs retain file targets by `turn_index`.
Recorded database file activity stays in `file_activity`, including when native
tools supply the JSON. `FileActivity` holds the path and tool name;
`FileActivity.provenance` identifies the native record type and preserves its
source turn index and first-seen time. Source indices never index `Session.events`,
and first-seen times do not assert modification times. File activity alone does
not populate `files_changed` or infer read/write actions.

`PermissionDecision.status` records whether a completion was observed (`pending`
or `completed`). `native_decision` retains the harness's result label verbatim;
a missing label stays `None`, and a pending request has no result label.
Recorded request IDs join permission lifecycle records, without inferred tool-call
ownership. These session facts do not automatically create `PermissionDenial`,
which is a normalized refusal summary attached to an evidenced tool call.
`ToolExecution` likewise represents a recorded execution start; adapters leave it
absent when only a request or result was recorded.
Unsupported tool schemas are listed in `unsupported_tools`; opaque inputs survive.
For replay, construct `CopilotSource`
directly from supplied rows, state records, and optional `image_inputs`.
State JSONL is read and decoded once per extraction. Image bytes are keyed by
recorded path, with media type inferred per reference; inline images remain in
the records. CLI selection and read-only connection ownership stay in orchestration.

## Public extractor stages

`session_log_extractors.extractor_contract.Extractor[Request, Source]` defines
`read_source(request) -> Source` and `interpret_source(source) -> Session | None`.
The request and source types remain specific to each harness. Readers snapshot
all required records/assets; interpreters do no source I/O and apply no export
limits. Selection, connection ownership, diagnostics, and writing remain in the
CLI. All implementations use the shared exporter rather than overriding export.

Claude, Codex, and Copilot implement this protocol in their public extractor adapters;
their typed `EXTRACTOR` instances are used by single-session and batch extraction.
The public reader and envelope functions remain compatible. Mypy checks conformance in CI, and `scripts/test_extractor_contract.py`
checks signature rejection, source-free interpretation, full results, and
non-mutating export using native fixtures. The package includes `py.typed` so
imports retain their annotations when checked outside the source directory.

Codex uses one native interpreter with ordered messages and calls. It retains
native identities, collaboration mode, commands, plan updates, skill names,
changed files, and patch completion records in named model fields. Tool results
match calls by recorded call ID, including across later human messages; unmatched
results remain explicit. Denial prose without a call ID stays on its recorded
message without a guessed tool association. No question-association or authorship
correction is included. `ToolCall.result_record.content` retains complete native
results, while `ToolCall.result` retains the existing uncapped textual
interpretation for JSON compatibility. Missing, null, and empty results stay
distinct. The exporter alone caps that textual view, without mutating either
representation. Unsupported tool inputs are retained and reported on stderr.
The duplicate `Turn`, `build_turns`, and `build_events` paths are removed; callers
use `interpret_source(CodexSource(...))` and `export_session` instead.

Copilot also implements the public contract and returns uncapped sessions.

## Shared session/export boundary

Adapters can supply `Session`, `Event`, `ToolCall`, and `Image` dictionaries from
`session_log_extractors.session_model` directly to
`session_log_extractors.session_log_envelope.export_session(session, tool_result_max_bytes=4096)`.
Supply complete interpreted content: export copies it before applying the existing
UTF-8 byte cap to textual tool results, retaining original byte counts and
truncation flags. Negative limits retain full text. Structured results, nested
payloads, images, ordering, and missing/null/empty values pass through unchanged.
The model has no presentation state, adapter base class, or runtime validation.

All three harnesses apply limits through `export_session`; CLI defaults are unchanged.

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
Ruff, mypy, Import Linter, and pre-commit are pinned in `pyproject.toml`; mypy
checks the entire source package in strict mode, including
`disallow_any_unimported` and `disallow_any_explicit`. All test dependencies are locked in
`uv.lock`. Format Python with `uv run --locked ruff format .`. Run the same
checks as CI before opening a PR:

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

Overall coverage excludes test modules. All five gates—overall, changed-line, Codex, Claude,
and Copilot coverage—must each reach 95%. Run diff-cover immediately after the overall test
run, which generates `coverage.xml`.

After intentionally changing dependencies in `pyproject.toml`, run `uv lock`
and commit `uv.lock` with the change. Use `uv lock --upgrade` only when intending
to upgrade the locked dependencies.

## Automated boundaries and review responsibilities

Install the local hooks with `uv run --locked pre-commit install`. Run all hooks
with `uv run --locked pre-commit run --all-files`. Hooks invoke the same locked
Ruff, mypy, and Import Linter commands as CI. Mypy and Import Linter always run
for the whole package, regardless of the staged filenames; Ruff checks Python
files passed by pre-commit.

Import Linter's declarative contracts in `pyproject.toml` prohibit direct and
indirect model dependencies on readers, extractors, or export, and exporter
dependencies on readers or extractors. Protected contracts restrict direct
imports of each harness schema module to its own extractor and the exact shared
union module. The union remains available to the model. Architecture regression
tests inject forbidden graph edges, including indirect dependencies, and verify
the configured contracts reject them. No custom lint rules or checker scripts
are needed.

Module dependencies cannot prove that interpretation avoids source access or
export caps when orchestration shares a module. Native-fixture contract tests
cover those behavioral boundaries for Claude and Copilot, including unknown
payload fields and non-mutating export. Codex protocol adoption is out of scope.

Review still judges names, readability, semantic ownership, recorded versus
derived values, fixture selection, and payload preservation. Review must also
reject new suppressions or weakened configuration and compare complexity inside
existing suppressed functions: automated tools do not detect that growth.
See [AGENTS.md](AGENTS.md) for these judgment-based rules.

## Type escape policy

The package has no casts or type ignores. Ruff's `TID251` bans `typing.cast`,
`typing_extensions.cast`, and both `no_type_check` APIs; `PGH003` requires coded
ignores. Mypy runs in strict mode with both `Any` restrictions across the whole source
package, requires error codes, and rejects unused ignores. Recursive JSON types
and checked narrowing preserve opaque values and unknown native fields.

Adding or broadening type-checker suppressions is prohibited by AGENTS.md and
checked in PR review alongside Ruff exceptions. Remove existing ignores when
fixing or replacing the code they cover; do not add new ones.

## Ruff lint policy and initial inventory

CI runs Ruff across application code, generators, and tests. E4/E7/E9 and F
catch basic Python errors; B catches common bugs; I orders imports; C4 and PIE
catch unnecessary collection/redundant patterns. PT011/PT012/PT017 strengthen
exception tests. PLC0415 requires module-level imports, including in tests.
PGH004 rejects blanket noqa comments and RUF100 rejects stale ones.
C901/PLR0915/PLR0912/PLR0913 enforce complexity/statements/branches/arguments at
10/50/12/5. These are Ruff's configurable defaults, not universal quality
standards. Lint supplements behavioral tests; it does not prove extraction
correctness or complete native-format coverage. UP/SIM adoption is deferred.

On the pinned Ruff 0.14.0 formatting baseline (`4f5b449`), the initial inventory
was **107 findings**: 30 C901, 21 PLR0912, 13 PLR0915, 4 PLR0913, 15 B011,
10 PT017, 9 I001, 4 B905, and 1 PIE810. The 39 non-complexity findings are fixed
(imports, exception tests, explicit zip behavior, and redundant startswith). The
initial 68 complexity findings were recorded as function-level, rule-specific
exceptions. The remaining exceptions exist
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
