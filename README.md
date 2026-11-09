# session-log-extractors

Reusable JSON extractors for Claude Code, Codex, and GitHub Copilot CLI sessions.
They preserve the recorded conversation, inline available images, and attach
structured facts supported by native evidence.

## Extract sessions

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run
`uv sync --locked` from the repository root. Python 3.11 or newer is supported.
The scripts use the installed package in `src/session_log_extractors/`.

```sh
uv run --locked python scripts/extract_claude_session_log.py
uv run --locked python scripts/extract_codex_session_log.py
uv run --locked python scripts/extract_copilot_session_log.py
```

By default, each command exports all sessions matching the current project.
The repository scripts write to the checkout root, including when invoked from
subdirectories. Filenames are `<harness>_session_log_raw_<session-id>.json`.
Upload those JSON files with their inline images; no image description pass or
hand editing is needed. Recorded Markdown remains message content.

Use `--help` on each script for its complete interface. Common options are:

| Selection or option | Behavior |
| --- | --- |
| Positional session ID | Export one session. A unique exact recorded ID wins; otherwise a unique literal prefix is required. Matching is case-sensitive. |
| `--all` | Export every matching project session; this is the default with no explicit selection. |
| `--strict` | Require the exact recorded working directory rather than parent/descendant project matching, and disable the global fallback. |
| `--output ./exports` | Choose a batch output directory. |
| `--output ./session.json` | Choose a single-session file destination. |
| `--output -` | Write one session to stdout; without a selector, choose the newest project match or fall back to the newest global session. Diagnostics stay on stderr. |
| `--tool-result-bytes N` | Cap textual tool results at N UTF-8 bytes; default **8,192**, zero clips nonempty text, any negative value retains full text. |

Claude and Codex also accept `--transcript PATH`. Codex accepts a transcript
path as its positional selection and `--sessions-root DIR` for discovery. Copilot accepts
`--db PATH` for its SQLite store; it also reads the corresponding native session
state and attachments. Copilot has no direct JSONL/state-root CLI flag.

For all three harnesses, `--output -` without a selector can export another
project's session when no project match exists. Use `--strict` or an explicit
selector when project isolation is required.

Explicit selection never falls back to a newest unrelated session. Missing or
ambiguous IDs fail with exit 1 and stderr diagnostics, without changing output
files. Copilot treats `%` and `_` as literal ID characters.

Claude and Codex ignore blank lines and skip malformed JSON/non-object records
with source/line diagnostics (up to five individual warnings plus a total).
Blank files and valid objects with no conversation activity produce `events: []`.
There is no native-format recognition gate, so unrelated JSON objects can also
produce an empty export. Nonblank input with no valid objects fails. Codex
discovery still needs recorded cwd metadata; select a blank file explicitly.
Copilot sessions without usable conversation likewise export an empty envelope.
Missing/unreadable sources and output failures exit 1. Batch mode attempts all
selected sessions and retains successful files; it exits 1 for any failure or
no selected sessions. Tolerated skipped records alone do not fail extraction.

Available images retain their owning message and order. Missing assets remain
explicit unavailable references, rather than disappearing. The exporter warns
above **8 MiB** of serialized JSON. The result cap is not a whole-file limit:
inputs, structured results, attached facts, conversation, and images stay complete.

### Docker helpers

Build from the repository root so the image includes the package:

```sh
docker build -f scripts/Dockerfile -t session-log-extractor .
docker run --rm session-log-extractor -c 'from session_log_extractors import session_log_envelope'
```

For mounted extraction, follow the repository helpers for
[Claude](.claude/commands/extract-claude-session-logs.md),
[Codex](.agents/skills/extract-codex-session-logs/SKILL.md), or
[Copilot](.agents/skills/extract-copilot-session-logs/SKILL.md).
They verify the checkout and mount its native sources read-only. Output must
stay inside the shared checkout, for example batch `--output ./exports`.
Destinations outside the checkout are unsupported by these workflows.
Single-session `--output` names a file, not a batch directory.

## Understand the output

The goal is to make the candidate’s deliberate actions and decisions visible:
what they said, what they chose, and the recorded outcomes. Classify activity by
its evidence rather than who generated its wording. Investigate unresolved
attribution without hiding meaningful activity or guessing when evidence is
insufficient. Examples illustrate these principles; they are not an exhaustive
list of candidate actions.

The envelope uses schema `raw-session-log/1`. Traverse `events` in recorded
order, with each message's tool calls/results, images, and attached facts.
Questions and answers use `interaction_questions` and `question_responses` on
their evidenced tool calls. Refusals, plans, actions, task prompts, and invocations
are attached to the messages, calls, or results they describe where supported.
They are separate from textual `result` and survive a zero-byte result cap.
Internal session indexes do not become top-level JSON collections.

Interpretation establishes ownership using recorded IDs, native containment,
and explicit relationships. Export copies established facts and applies output
policy; it does not reconstruct associations from internal indexes or proximity.
Unassociated evidence stays explicit. Claude and Copilot can retain orphan
responses on result-only conversation activity; Codex unmatched results without
a conversation owner remain internal. Consumers must not assume every internal
fact appears in JSON.

Authorship is independent of message role. Candidate activity includes supported
native notices of deliberate control actions, even when the harness generated
the wording. Automatically supplied context and primary-assistant output remain
distinct from candidate input; another agent requires recorded identity.
Unknown attribution stays unknown. Original text, wrappers, and whitespace remain
available. See the [preservation guide](scripts/fixtures/README.md) for acceptance
examples and evidence limits, and [architecture](docs/architecture.md) for the
pipeline and responsibility boundaries.

Tool failures, cancellation, settings refusals, and human rejection are different
facts. Missing decisions never imply approval. Assistant prose alone does not
establish a permission refusal. Successful execution or an attempted edit does
not prove a file changed; use evidenced action confirmation rather than a legacy
`files_changed` list. Definitions and skill links alone do not prove invocation.
No external skill/command definitions are loaded.

The [shared model](src/session_log_extractors/session_model.py) and
[exporter](src/session_log_extractors/session_log_envelope.py) are authoritative
for exact types and exported fields. Export does not perform general runtime
semantic validation or repair relationships. Additive fields retain the schema
version; incompatible removal or changes of type/meaning require a new version.
There is no version negotiation.

## Capture compatibility

Tests select one current complete versioned native capture per harness. See
[Claude provenance](scripts/fixtures/claude/README.md),
[Codex provenance](scripts/fixtures/codex/README.md), and
[Copilot provenance](scripts/fixtures/copilot/README.md) for supported evidence,
controlled boundaries, and capture-specific limitations.

Select evidence by tool names, structured properties, prepared assets, or
operator markers, then associate it through recorded IDs. Generated assistant
prose, question wording, labels, shell syntax, and model names may change without
requiring rewritten prose assertions or approval of a new output baseline.
Keep precise checks for controlled parser cases, payload preservation, ownership,
ordering, images, decisions, and byte limits. Missing required feature evidence
must fail explicitly.

When an upgrade changes native behavior, retain a focused legacy case with its
version and reason where needed; keep the main suite on the selected current
capture. Extractor bugs require fixes. Approved whole-output baselines and an
automated fresh-capture drift workflow are unfinished. Ordinary tests use
committed inputs and never run paid capture. Future capture uses the
[capture-native-session skill](.agents/skills/capture-native-session/SKILL.md);
the scripted generators and fake-CLI support tests have been retired.

## Public extraction stages

All three public adapters implement
[`Extractor[Request, Source]`](src/session_log_extractors/extractor_contract.py).
`read_source` snapshots required evidence/assets; `interpret_source` returns an
uncapped shared `Session` without source I/O, including empty sessions. CLI
orchestration owns selection, connections, diagnostics, and writing. Use
`export_session(session, tool_result_max_bytes=8192)` for JSON output.
The [native-fixture contract tests](scripts/test_extractor_contract.py) cover all
three harnesses, source-free interpretation, and non-mutating export. The
package ships `py.typed` for typed consumers.

Child evidence differs by harness: Claude has sidecar readers and summaries;
main JSON omits those sidecars. Codex child sources can be read, interpreted, and
exported separately, respecting their recorded history boundary. Copilot child
records are inline with agent scope, with separate summaries available internally.
A parent's task summary is not the child's conversation.

### Deterministic capture sanitization

`capture_sanitization.py` contains the capture redaction rules, separately from
extraction. It reads no environment variables, credentials, home directory, or
clock. Identical input bytes and explicit rules produce identical sanitized
bytes and count/hash reports. Use this command to prepare and review interactive
capture copies.

Create a private rules file outside the repository, for example:

```json
{
  "literal_replacements": [
    {"name": "home", "original": "/Users/example", "replacement": "/Users/PILOT_USER"},
    {"name": "host", "original": "example-mac", "replacement": "PILOT_HOST"},
    {"name": "known_secret", "original": "explicit-sensitive-value", "replacement": "[REDACTED_SECRET]"}
  ],
  "redact_emails": true,
  "redact_api_keys": true
}
```

```sh
uv run --locked python scripts/sanitize_capture_cli.py /tmp/raw-session.jsonl \
  --rules /tmp/private-rules.json --format jsonl \
  --output /tmp/sanitized-session.jsonl --report /tmp/sanitization-report.json
```

The public Python entrypoint is `sanitize_capture(text, rules, format="jsonl")`
in `session_log_extractors.capture_sanitization`. Formats are `text`, `json`, and
`jsonl`. JSON formats parse string values before replacement, preserving record
order, keys, scalar types, and inline image data. They normalize JSON whitespace
and LF line endings; text mode retains existing formatting. Native IDs and
timestamps remain unless an explicit rule matches their values.
In JSON formats, objects with `type: "credential_org"` always have their
`organizationUuid` replaced with `[REDACTED_ORGANIZATION_ID]`, without requiring
the original value in configuration. Each newly redacted field is reported as
an `organization_id` match; existing placeholders are left unchanged.
Original files are never overwritten by the CLI, including through symbolic or
hard links.

Literal replacements are case-sensitive and take priority over built-in
patterns, with longer literals first. Replacement text is not processed again
in the same pass. The named email pattern matches all email-like strings,
including public attribution addresses; the API-key pattern matches
`sk-ant-` values and displayed fragments. Other secrets require explicit literal
rules. JSON keys and binary assets are outside text redaction; inline image
payloads are preserved byte-for-byte. This is a configured sanitizer, not a
general secret detector.

Reports contain SHA-256 hashes and counts for every enabled rule, including zero
matches, sorted by rule name. They contain no matched values, timestamps, or
machine-specific paths. With no `--report`, the report goes to stdout. Invalid
rules or malformed JSON fail explicitly rather than silently dropping records.

## Development

Install locked development tools with `uv sync --locked --extra test` (CI pins uv
0.12.11). Add `--python 3.11` to match CI. uv manages `.venv` without activation.
After intentional dependency changes, run `uv lock` and commit the lockfile.

Follow [AGENTS.md](AGENTS.md) for the authoritative commands and coding policy.
Before committing, run its Ruff format/check, strict package-wide mypy, Import
Linter, and all **five 95% coverage gates**: overall, changed-line, and separate
Claude, Codex, and Copilot extractor coverage. Run diff-cover after the overall
run creates `coverage.xml` and after fetching `origin/main`.
Do not add suppressions, broaden exceptions, weaken settings, or lower thresholds.

Install hooks with `uv run --locked pre-commit install`; run them with
`uv run --locked pre-commit run --all-files`. Hooks and
[CI](.github/workflows/test.yml) use the locked tool versions. Import Linter
checks dependency directions; native-fixture tests check behavioral boundaries.
Review still assesses semantic ownership, preservation, names, and readability.
See the [historical implementation plan](docs/implementation-plan.md) for migration
rationale, rather than current contributor instructions.

## Ownership

This repository is owned and maintained by Andela. The source is published
here for transparency, but **no license to use, copy, modify, or redistribute
it is granted**. All rights are reserved by Andela.

## Releases

Releases are tagged with immutable semantic versions, e.g. `v1.0.0`.
