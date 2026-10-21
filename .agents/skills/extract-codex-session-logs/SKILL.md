---
name: extract-codex-session-logs
description: Manual-only helper for exporting all Codex session logs for a candidate clone of this repository by running scripts/extract_codex_session_log.py (via Docker, with --all). Writes one JSON envelope per session with images inlined as base64 for upload. Use only when explicitly invoked as $extract-codex-session-logs or selected from the skills UI.
---

# Extract Codex Session Logs

Run the Codex session-log extractor for every Codex transcript associated with the current candidate clone of this repository. It runs in Docker (see [scripts/Dockerfile](../../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

1. Resolve the current Git root with `git rev-parse --show-toplevel`. Verify that the root directory contains `scripts/extract_codex_session_log.py` — this repo's checkouts (the maintainer repo itself, and any candidate clone of it, regardless of naming) all carry that script at their root.
2. If `scripts/extract_codex_session_log.py` is not there, stop and report that the current directory is not a checkout of this repo. Do not search for or extract the newest Codex session from another repository.
3. Verify `docker` is available (`command -v docker`). If it is not, stop and tell the user to install Docker (or start Docker Desktop) and try again.
4. Set the command working directory to that verified clone root and run:

```bash
image="rfp-monster-session-log-extractor:local"
docker build -q -f scripts/Dockerfile -t "$image" . >/dev/null
mounts=(-v "$PWD:$PWD" -v /tmp:/tmp:ro)
[ -d "$HOME/.codex/sessions" ] && mounts+=(-v "$HOME/.codex/sessions:$HOME/.codex/sessions:ro")
docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$PWD" "${mounts[@]}" "$image" \
  "$PWD/scripts/extract_codex_session_log.py" --all
```

The container only gets read-only access to `~/.codex/sessions` (not the user's whole home directory), read-only access to `/tmp` so temporary image sources can be inlined, and read-write access to this checkout — nothing else on the machine is exposed to it.

5. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--sessions-root DIR`, and `--strict` (only export sessions started in this exact directory — no parent/descendant cwd overlap, no newest-anywhere fallback). `--tool-result-bytes N` changes the tool-result cap (negative for no cap).
6. Record the JSON files written from the command output for the final report.

This checkout check is mandatory. If extraction reports no matching transcripts, stop and say so; do not re-run it from a broader directory or without the repository scoping.

## What gets written

One file per session: `codex_session_log_raw_<identifier>.json`. It carries the full conversation, full tool inputs, tool results capped at 4 KB with their original size recorded, and pasted images inlined as base64. Recorded Markdown remains message content inside the JSON.

Codex stores images out of band — as a filesystem path, a data URL, or raw base64 — so the envelope is what makes them portable. An image that will not resolve (most often a path whose file has since been deleted) is recorded in the envelope as `{"unavailable": true, "ref": ...}` rather than dropped, so "pasted a screenshot we can no longer read" stays distinguishable from "pasted nothing".

## Upload

Tell the user to drag each `codex_session_log_raw_<identifier>.json` file into Qualified. Upload only the generated JSON files; no image description pass or hand edits are needed.
