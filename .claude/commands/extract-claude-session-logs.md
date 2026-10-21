---
description: Extract all Claude Code session logs for this project
allowed-tools: Bash(docker build:*), Bash(docker run:*), Bash(git rev-parse:*), Bash(command -v docker:*)
---

Run the session log extractor and report what it wrote. It runs in Docker (see [scripts/Dockerfile](../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

!`r=$(git rev-parse --show-toplevel 2>/dev/null || pwd); if [ ! -f "$r/scripts/extract_claude_session_log.py" ]; then echo "no scripts/extract_claude_session_log.py under $r — not inside a checkout of this repo (or a candidate clone of it); nothing to extract"; elif ! command -v docker >/dev/null 2>&1; then echo "docker is required to run the extractor — install Docker (or start Docker Desktop) and try again"; else image="rfp-monster-session-log-extractor:local"; docker build -q -f "$r/scripts/Dockerfile" -t "$image" "$r" >/dev/null; mounts=(-v "$r:$r"); [ -d "$HOME/.claude/projects" ] && mounts+=(-v "$HOME/.claude/projects:$HOME/.claude/projects:ro"); docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$r" "${mounts[@]}" "$image" "$r/scripts/extract_claude_session_log.py" $ARGUMENTS; fi`

**Scoping.** Run from inside this repo's checkout, a candidate clone of it, or a subdirectory of either, the extractor resolves your session from your working directory alone — no clone-naming convention required. Run from anywhere else (no `scripts/extract_claude_session_log.py` at the resolved git root), it does *not* fall back to whatever session is newest anywhere on disk — it prints the message above and does nothing. If that happens, stop and say so; do not re-run without the scoping.

The container gets read-only access to `~/.claude/projects` plus read-write access to this checkout.

**What the container runs.** The image supplies Python and the installed shared exporter package. The
script it runs is this checkout's own `scripts/extract_claude_session_log.py`
(bind-mounted at the same path), not the copy baked into the image under
`/extractor/scripts/`. Running the checkout's copy lets the script locate the checkout
root from its own path, so it writes JSON there without being passed
`--output`. Passing `--output "$r"` would break single-session runs, because in
that mode `--output` names the JSON file, not a directory.

Defaults to `--all`, writing one JSON file per session into the repo root:
`claude_session_log_raw_<session-id>.json`. Each envelope carries the conversation,
full tool inputs, tool results capped at 4 KB with their original size recorded,
and pasted images inlined as base64. Markdown inside recorded messages is preserved.

Pass-through args (optional): `--output DIR` for a different directory, `--strict`
to only export sessions started in this exact directory (no parent-dir walk, no
newest-anywhere fallback), or a single session id / `--output -` to extract just
one. For a single session, `--output PATH` names the JSON file and `--output -`
writes JSON to stdout; diagnostics go to stderr. `--tool-result-bytes N`
changes the tool-result cap (negative for no cap). It replaces
`--raw-tool-result-bytes`; the `--raw` and `--no-raw` flags have been removed. For Codex transcripts use
`scripts/extract_codex_session_log.py` instead, and for Copilot use
`scripts/extract_copilot_session_log.py`.

Tell the user which JSON files were written and to upload those files into
Qualified. Images are already included; no image-description pass or hand editing
is needed.
