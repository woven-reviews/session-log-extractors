---
name: extract-copilot-session-logs
description: Manual-only helper for exporting all GitHub Copilot CLI session logs for a checkout of this repo by running scripts/extract_copilot_session_log.py (via Docker, with --all), writing JSON envelopes with available images inlined as base64 for upload. Use only when explicitly invoked as $extract-copilot-session-logs or selected from the skills UI.
---

# Extract Copilot Session Logs

Run the Copilot session-log extractor for every Copilot CLI session associated with the current checkout of this repo, and upload the generated JSON envelopes. It runs in Docker (see [scripts/Dockerfile](../../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

1. Resolve the current Git root with `git rev-parse --show-toplevel`. Verify that the root directory contains `scripts/extract_copilot_session_log.py` — this repo's checkouts (the maintainer repo itself, and any candidate clone of it, regardless of naming) all carry that script at their root. If it is not there, stop and report that the current directory is not a checkout of this repo; do not search for or extract a session from another repository.
2. Verify `docker` is available (`command -v docker`). If it is not, stop and tell the user to install Docker (or start Docker Desktop) and try again.
3. Set the command working directory to that verified root and run:

```bash
image="rfp-monster-session-log-extractor:local"
docker build -q -f scripts/Dockerfile -t "$image" . >/dev/null
mounts=(-v "$PWD:$PWD" -v /tmp:/tmp:ro)
[ -d "$HOME/.copilot" ] && mounts+=(-v "$HOME/.copilot:$HOME/.copilot:ro")
docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$PWD" "${mounts[@]}" "$image" \
  "$PWD/scripts/extract_copilot_session_log.py" --all --output "$PWD"
```

The container only gets read-only access to `~/.copilot` (not the user's whole home directory), read-only access to `/tmp` for referenced attachments, and read-write access to this checkout — nothing else on the machine is exposed to it.

The command runs the verified checkout's script, whose project root is the checkout. `--output "$PWD"` also sets the destination explicitly.

4. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--db PATH`, and `--strict` (only export sessions started in this exact directory). `--tool-result-bytes N` changes the tool-result cap (negative for no cap).
5. Record the files written from the command's stderr diagnostics and report any skipped sessions or warnings.

## What gets written

One `copilot_session_log_raw_<identifier>.json` file per session. Each envelope contains session metadata, ordered messages, tool calls with full inputs, and results capped at the configured byte limit with original size and truncation metadata.

Copilot records images in the SQLite attachments table or per-session events log. Available image bytes are inlined as base64. A reference without a readable source remains explicit as `{"unavailable": true, "ref": ...}`. Do not hand-edit the generated JSON or add image descriptions.

## Upload

Tell the user to drag each generated `.json` file into Qualified. Report skipped sessions and unavailable image references without claiming those images were recovered.
