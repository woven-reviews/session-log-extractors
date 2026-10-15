# Codex fixture provenance

These native fixtures cover the headless-generatable portions of the ten shapes recorded in [QUAL-2400](https://linear.app/andela/issue/QUAL-2400/codex-test-coverage). Captured on 2026-10-15 with Codex CLI 0.162.1. There are **no hand-authored JSONL fixtures**. Tests read committed files and require no local Codex login or session directory.

## Regenerate

Run `python3 scripts/generate_codex_fixtures.py` from the repository root with a logged-in `codex` CLI. It makes model calls. Each case runs in a temporary workspace with `codex exec --json --ignore-user-config`. The model case resumes its first thread under `gpt-6-luna`. The image case attaches `pixel.png`, a 64×64 synthetic test asset enlarged from the existing one-pixel Copilot fixture; the 1×1 source was too small for this Codex CLI to process. The script finds only the new thread IDs, retains relevant native records, strips startup instructions and unrelated tool output, redacts home/workspace paths, checks each capture's shape, and stages all outputs before replacement. Session IDs and timestamps vary on regeneration. Review the diff before committing.

| QUAL-2400 shape | Classification and evidence |
| --- | --- |
| 1. Slash commands | **Headless-generatable:** bare `/review fixture` in `slash.jsonl`. **Fixture-only:** the interactive `<command-name>` wrapper cannot be emitted by `codex exec`; its narrow parser test remains inline. |
| 2. Plan mode | **Fixture-only:** headless exec has no Plan-to-Default UI transition or structured `update_plan` exchange. Existing inline test covers it. |
| 3. Permission denial | **Fixture-only:** an unattended run cannot supply a human rejection with feedback. Existing inline test covers it. |
| 4. `request_user_input` answer | **Fixture-only:** headless execution cannot answer an interactive options prompt. Existing inline test covers it. |
| 5. Pasted images | **Headless-generatable:** `image.jsonl` contains a native base64 `input_image` block and image path; tests decode it using committed `pixel.png`. **Fixture-only:** a deleted backing file requires deliberate mutation; the missing-path test stays inline. |
| 6. `apply_patch` changes | **Headless-generatable:** `tool_patch_large.jsonl` contains the native `FileChange` and `custom_tool_call` records. The extractor reads completed `FileChange` paths and patch output plus native tool calls/results; older `patch_apply_end` and `function_call` parser tests remain inline for their distinct schema. |
| 7. Model switch | **Headless-generatable:** `model.jsonl` has two turns and two native `turn_context.model` values after `codex exec resume -m gpt-6-luna`. |
| 8. Multi-session scan | **Headless-generatable:** the four captures have distinct native session IDs and cwd metadata. **Fixture-only:** literal `~`, symlink, overlap, and nonmatch arrangements need controlled path values; the selection tests retain their temporary paths. |
| 9. Malformed JSONL | **Fixture-only:** a completed headless run cannot intentionally end with a truncated line. The loader test keeps its small inline partial line. |
| 10. Large tool result | **Headless-generatable:** `tool_patch_large.jsonl` contains thirty repeated tool-output lines. The native result is checked for truncation at a 64-byte limit; older `function_call_output` behavior remains covered inline. |

The extractor handles the captured native tool and patch records while retaining the older parser branches. Verify with `pytest -q scripts/test_extract_codex_session_log.py scripts/test_generate_codex_fixtures.py` and both coverage gates in `AGENTS.md`.
