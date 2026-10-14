# Copilot fixture provenance

These fixtures cover the audited shapes in [QUAL-2401](https://linear.app/andela/issue/QUAL-2401/copilot-test-coverage) and [QUAL-2404](https://linear.app/andela/issue/QUAL-2404/copilot-fixture-generation). Native data was captured on 2026-10-14 with GitHub Copilot CLI 1.0.90-5. Tests read only committed files here; they need neither a Copilot login nor `~/.copilot`.

## Regenerate native captures

Run `python3 scripts/generate_copilot_fixtures.py` from the repository root with a logged-in `copilot` CLI. This is an on-demand operation that makes model calls. The script creates a temporary workspace per case and invokes `copilot -p <prompt> --allow-all-tools --no-custom-instructions --disable-builtin-mcps --no-auto-update --no-remote-export --silent -C <workspace> --session-id <uuid>`. It adds `--attachment <pixel.png>` for the image case and uses `--resume <uuid>` for the second continued turn. Prompts and shape checks are in the script. The CLI writes its native `~/.copilot/session-store.db` and `~/.copilot/session-state/<uuid>/events.jsonl`; the script copies only its six new session IDs into a new SQLite database, sanitizes the corresponding JSONL, and stages every output before replacement. `manifest.json` maps stable case names to capture UUIDs. IDs and timestamps vary on regeneration, while the test-visible shapes are reproducible. Review the diff before committing new captures.

The native database retains Copilot's table definitions and rows for `sessions`, `turns`, `checkpoints`, `session_files`, `session_refs`, and `assistant_usage_events`. It contains no other local sessions. The sanitizer replaces the temporary workspace with `/workspace`, the operator home with `/home/fixture-user`, and email addresses with `fixture@example.invalid`. It omits `session.usage_checkpoint` and `system.message` records because they contain account diagnostics and system prompts unrelated to extraction. Other native event records remain in order. The generator fails if a required tool, skill, attachment, or resumed turn is missing.

| Native case | File | Capture |
| --- | --- | --- |
| Baseline session and DB turn | `baseline/events.jsonl` | One headless answer with `BASELINE_FIXTURE_MARKER` |
| Continued conversation and multiple turns | `continued/events.jsonl` | Resume the same session and recall `BLUE-WREN-17` |
| Tool request and successful completion | `tool_success/events.jsonl` | Run `printf TOOL_FIXTURE_MARKER` through `bash` |
| Shell command with nonzero exit | `tool_failure/events.jsonl` | Run `false` through `bash`; CLI 1.0.90-5 records `success: true` for the tool wrapper and exit code 1 in its result |
| Skill invocation and path | `skill/events.jsonl`, `fixture-skill.md` | Load a temporary `.github/skills/fixture-skill/SKILL.md` |
| Image attachment and native asset record | `image/events.jsonl`, `pixel.png` | Pass a one-pixel PNG with `--attachment` |

## Audited cases requiring hand-authored data

The `hand-authored/` JSONL files contain parser edge cases that a completed, unattended CLI run cannot reliably produce. Some tests use small inline values or modify a copy of the native SQLite database for a specific branch; those values are hand-authored test data, not claims of native provenance.

| QUAL-2401 shape or test family | Data and reason |
| --- | --- |
| Approval, denial with human feedback, and pending request | `hand-authored/permissions.jsonl`. Headless mode requires `--allow-all-tools`; it cannot supply interactive feedback or leave an approval awaiting a human. |
| Malformed/truncated JSONL, wrong data types, orphan completion | `hand-authored/malformed.jsonl`. A completed CLI run cannot intentionally produce a partial write or impossible event sequence. Tests feed the same file to attachment, permission, skill, and tool readers. |
| Attachment `path`, `dataUrl`, inline `data` + `mediaType`, and no attachment | `hand-authored/attachment_variants.jsonl`. CLI 1.0.90-5 emits a filesystem path for `--attachment`; it does not emit the two inline encodings through headless mode. The encoded bytes come from the native capture's `pixel.png`. |
| Image token resolved through DB, through state events, or unresolved | Native `image/events.jsonl` supplies the state attachment record and `pixel.png`; tests add `[image: pixel.png]` in a copied native DB. Headless `--attachment` does not put that token into the DB turn. The legacy `attachments` table and missing source are simulated in tests. |
| Missing file, corrupt base64, extension inferred from MIME | Inline edge values and the committed `pixel.png`. These represent deleted or malformed sources that a successful capture cannot guarantee. |
| DB cwd overlap, nonmatch fallback, strict no-match, multiple sessions, empty DB | Tests use `session-store.db` directly or copy it and adjust `cwd`/rows. Native capture has six sessions; exact missing and overlapping paths need deterministic edits. |
| Missing `turn_index`, shell `<bash-input>`, empty or populated refs/files/checkpoints, model duplicates/NULL | Inline rows in tests. CLI 1.0.90-5 supplies the base schema, but it has no `events` table (used by an older model lookup path) and does not reliably produce the other legacy/invalid rows. |
| DB `attachments` table present versus absent | Current native schema has no `attachments` table. Tests add it only for the legacy branch and otherwise use the captured absent-table schema. |
| Skill event without a path, tool `success: false`, parser type fallbacks | Inline test values. The native skill event contains a path; a shell command exiting 1 is still recorded as a successful tool wrapper. The invalid-type cases require deliberate mutation. |
| CLI errors, OS errors, rendering branch combinations, helper inputs | Small inline scalars, temporary paths, and monkeypatches in `test_extract_copilot_session_log.py`. These test deliberate failures or combinations of values rather than a complete CLI session. |

`test_extract_copilot_session_log.py` reads the native DB and event fixtures for ordinary DB retrieval, UUID prefix lookup, skill and tool parsing, attachment rendering, and multi-session extraction. Its shared DB builder copies the native schema before adding the hand-authored rows needed for individual edge tests. The extractor itself is unchanged.

Verify without Copilot: `pytest -q scripts/test_extract_copilot_session_log.py scripts/test_generate_copilot_fixtures.py`, then `pytest --cov --cov-fail-under=95`.
