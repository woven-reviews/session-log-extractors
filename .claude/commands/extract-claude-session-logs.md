---
description: Extract all Claude Code session logs for this project
allowed-tools: Bash(docker build:*), Bash(docker run:*), Bash(git rev-parse:*), Bash(command -v docker:*), Read, Edit
---

Run the session log extractor and report what it wrote. It runs in Docker (see [scripts/Dockerfile](../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

!`r=$(git rev-parse --show-toplevel 2>/dev/null || pwd); if [ ! -f "$r/scripts/extract_claude_session_log.py" ]; then echo "no scripts/extract_claude_session_log.py under $r — not inside a checkout of this repo (or a candidate clone of it); nothing to extract"; elif ! command -v docker >/dev/null 2>&1; then echo "docker is required to run the extractor — install Docker (or start Docker Desktop) and try again"; else image="rfp-monster-session-log-extractor:local"; docker build -q -t "$image" "$r/scripts" >/dev/null; mounts=(-v "$r:$r" -v /tmp:/tmp); [ -d "$HOME/.claude/projects" ] && mounts+=(-v "$HOME/.claude/projects:$HOME/.claude/projects:ro"); docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$r" "${mounts[@]}" "$image" /extractor/extract_claude_session_log.py --output "$r" $ARGUMENTS; fi`

**Scoping.** Run from inside this repo's checkout, a candidate clone of it, or a subdirectory of either, the extractor resolves your session from your working directory alone — no clone-naming convention required. Run from anywhere else (no `scripts/extract_claude_session_log.py` at the resolved git root), it does *not* fall back to whatever session is newest anywhere on disk — it prints the message above and does nothing. If that happens, stop and say so; do not re-run without the scoping.

The container only gets read-only access to `~/.claude/projects` (not your whole home directory), plus read-write access to this checkout and `/tmp` (where pasted images get dumped) — nothing else on your machine is exposed to it.

Defaults to `--all`, writing **two files per session** into the repo root:

| File | What it is |
|---|---|
| `session_log_<session-id>.md` | The readable turn-by-turn log. Condensed: tool calls become one bullet each, results become a short note, images become a text description. |
| `claude_session_log_raw_<session-id>.json` | The raw envelope. Full conversation, full tool inputs, tool results capped at 4 KB with their original size recorded, and every pasted image inlined as base64. |

Note the naming: only the raw file is prefixed `claude_`. The markdown keeps its unprefixed `session_log_` name because the downstream consumer that selects it matches only the `codex_` and `copilot_` prefixes — renaming it would make it invisible.

Pass-through args (optional): `--output DIR` for a different directory, `--strict` to only export sessions started in this exact directory (no parent-dir walk, no newest-anywhere fallback), or a single session id / `--output -` to override and extract just one. `--no-raw` skips the envelope; `--raw-tool-result-bytes N` changes the tool-result cap (negative for no cap). For Codex transcripts use `scripts/extract_codex_session_log.py` instead, and for Copilot `scripts/extract_copilot_session_log.py`.

**Then describe any dumped images.** The extractor can't see images, so it dumps each pasted image to a tmp file and leaves a marker in the log:
`[Image dumped to `<path>` — description pending]`. For every such marker in the files just written, `Read` the image at `<path>`, then `Edit` the marker line to replace it with `[Image: <one- or two-sentence description>. Text: <any text visible in the image, or "none">]`. Skip this pass if no markers are present.

This pass applies to the **markdown only**. The raw envelope already carries the real image bytes, so it needs no description pass — and it must not be hand-edited.

**Finally, tell the user to upload both files.** Drag *both* the `session_log_<session-id>.md` and the `claude_session_log_raw_<session-id>.json` into Qualified. The markdown is what gets scored; the raw envelope is what lets a human grader see the images and the agent's actual output. Uploading only one of them loses half the picture.
