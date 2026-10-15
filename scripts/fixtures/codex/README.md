# Codex fixture provenance

These fixtures cover the ten shapes recorded in [QUAL-2400](https://linear.app/andela/issue/QUAL-2400/codex-test-coverage). Native logs were captured on 2026-10-15 with Codex CLI 0.162.1 through `codex exec --json --ignore-user-config`. The committed tests read only files here; no Codex login or local session directory is required.

## Regeneration

Run `python3 scripts/generate_codex_fixtures.py` from the repository root with a logged-in Codex CLI. It makes model calls and replaces the three native JSONL files only after all captures succeed. It creates separate temporary workspaces, locates each new transcript by the CLI's thread ID, retains extractor-relevant native records, removes startup instructions and unrelated tool output, replaces workspace and home paths, and rejects remaining private paths. Session IDs and timestamps vary. Review the diff and all captured content before committing. The one-pixel input comes from the already committed `scripts/fixtures/copilot/pixel.png`.

The native files are `slash.jsonl`, `tool_patch_large.jsonl`, and `image.jsonl`. They show what the current CLI actually writes. In 0.162.1, headless tools are `custom_tool_call` records and patches appear as `FileChange` items; the extractor's audited legacy branches expect `function_call` and `patch_apply_end`. The image attempt logged a path and “image content omitted because it could not be processed,” so it is evidence of the headless limit, not a successful image payload.

| QUAL-2400 shape | Classification and reason | Committed data |
| --- | --- | --- |
| 1. Bare and tagged slash commands | **Fixture-only as a pair.** Headless `/review fixture` is recorded as an ordinary user message; it does not emit the interactive `<command-name>` wrapper. | Native `slash.jsonl`; `hand-authored/slash_tagged.jsonl` for the wrapper. |
| 2. Plan mode enter/update/exit | **Fixture-only.** `codex exec` has no interactive Plan mode transition or structured `update_plan` exchange. | `hand-authored/plan_mode.jsonl`. |
| 3. Permission denial | **Fixture-only.** Unattended execution cannot provide a human denial with feedback. | `hand-authored/permission_denial.jsonl`. |
| 4. `request_user_input` answer | **Fixture-only.** Headless runs cannot answer an interactive options prompt. | `hand-authored/options.jsonl`. |
| 5. Local, base64, and missing pasted images | **Fixture-only for the audited variants.** The headless `-i` attempt recorded a path and omitted image content; a deleted source cannot be created by a completed run. | Native `image.jsonl` shows the limit; `hand-authored/images.jsonl` uses the committed one-pixel PNG bytes and a missing path. |
| 6. `apply_patch` file changes | **Fixture-only for the audited legacy schema.** Headless generated a real patch, but 0.162.1 logged `FileChange` and `custom_tool_call`, which the current extractor does not interpret. | Native `tool_patch_large.jsonl`; `hand-authored/tool_patch_large.jsonl` exercises the audited schema. |
| 7. Model switch mid-session | **Fixture-only.** A first model turn was headless-generatable, but resuming it under a second model failed in this CLI environment's sandbox before a second turn was written. | `hand-authored/model_switch.jsonl`. |
| 8. Multi-session scan with path variants | **Fixture-only for exact path variants.** Headless runs produce separate native session IDs and cwd values, but literal `~`, symlinked paths, and the required overlap/nonmatch arrangement need controlled fixture edits. | Three native session metadata records; `hand-authored/session.jsonl` is copied with deterministic cwd edits for selection tests. |
| 9. Malformed or truncated JSONL | **Fixture-only.** A successful headless run cannot intentionally end in a partial JSON write. | `hand-authored/malformed.jsonl`. |
| 10. Large tool result | **Fixture-only for the audited legacy schema.** A headless tool produced repeated output, but 0.162.1 records it as `custom_tool_call_output`; the extractor's truncation branch consumes `function_call_output`. | Native `tool_patch_large.jsonl`; `hand-authored/tool_patch_large.jsonl`. |

No complete audited shape is purely headless-generatable on this CLI version; three have native subsets. None are unsupported because fixture-only data covers the remaining extractor behavior.

The hand-authored files are intentionally small and use the shapes the extractor already supports. Inline tests remain for narrow error paths, temporary filesystem behavior, and helper fallbacks. Verify with `pytest -q scripts/test_extract_codex_session_log.py scripts/test_generate_codex_fixtures.py` and the repository's coverage commands in `AGENTS.md`.
