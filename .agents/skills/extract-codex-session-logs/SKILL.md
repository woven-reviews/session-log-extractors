---
name: extract-codex-session-logs
description: Manual-only helper for exporting all Codex session logs for a candidate clone of this repository by running scripts/extract_codex_session_log.py (via Docker, with --all), then replacing dumped-image markers with brief image descriptions. Writes both a readable markdown log and a raw JSON envelope with images inlined as base64; both get uploaded. Use only when explicitly invoked as $extract-codex-session-logs or selected from the skills UI.
---

# Extract Codex Session Logs

Run the Codex session-log extractor for every Codex transcript associated with the current candidate clone of this repository, then describe any pasted images that were dumped by the extractor. It runs in Docker (see [scripts/Dockerfile](../../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

1. Resolve the current Git root with `git rev-parse --show-toplevel`. Verify that the root directory contains `scripts/extract_codex_session_log.py` — this repo's checkouts (the maintainer repo itself, and any candidate clone of it, regardless of naming) all carry that script at their root.
2. If `scripts/extract_codex_session_log.py` is not there, stop and report that the current directory is not a checkout of this repo. Do not search for or extract the newest Codex session from another repository.
3. Verify `docker` is available (`command -v docker`). If it is not, stop and tell the user to install Docker (or start Docker Desktop) and try again.
4. Set the command working directory to that verified clone root and run:

```bash
image="rfp-monster-session-log-extractor:local"
docker build -q -t "$image" scripts >/dev/null
mounts=(-v "$PWD:$PWD" -v /tmp:/tmp)
[ -d "$HOME/.codex/sessions" ] && mounts+=(-v "$HOME/.codex/sessions:$HOME/.codex/sessions:ro")
docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$PWD" "${mounts[@]}" "$image" \
  /extractor/extract_codex_session_log.py --all
```

The container only gets read-only access to `~/.codex/sessions` (not the user's whole home directory), plus read-write access to this checkout and `/tmp` (where pasted images get dumped) — nothing else on the machine is exposed to it.

5. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--sessions-root DIR`, and `--strict` (only export sessions started in this exact directory — no parent/descendant cwd overlap, no newest-anywhere fallback). `--no-raw` skips the raw envelope; `--raw-tool-result-bytes N` changes the tool-result cap (negative for no cap).
6. Record the files written from the command output, then complete the image description pass below before sending the final report.

This checkout check is mandatory. If extraction reports no matching transcripts, stop and say so; do not re-run it from a broader directory or without the repository scoping.

## What gets written

Two files per session:

| File | What it is |
|---|---|
| `codex_session_log_<identifier>.md` | The readable turn-by-turn log. Condensed: tool calls become one bullet each, results become a short note, images become a text description. |
| `codex_session_log_raw_<identifier>.json` | The raw envelope. Full conversation, full tool inputs, tool results capped at 4 KB with their original size recorded, and every pasted image inlined as base64. |

Codex stores images out of band — as a filesystem path, a data URL, or raw base64 — so the envelope is what makes them portable. An image that will not resolve (most often a path whose file has since been deleted) is recorded in the envelope as `{"unavailable": true, "ref": ...}` rather than dropped, so "pasted a screenshot we can no longer read" stays distinguishable from "pasted nothing".

## Image Description Pass

The extractor is mechanical and cannot describe images itself. When a pasted image is present, it dumps the image to a temp file and writes this stable marker into the generated markdown:

```markdown
[Image dumped to `<path>` — description pending]
```

After extraction, inspect the generated `codex_session_log_*.md` files that were just written. For every marker line:

1. Extract the image path between the backticks.
2. Inspect that local image with an image-capable viewer/tool.
3. Replace every occurrence of that same marker in the generated markdown with:

```markdown
[Image: <one- or two-sentence description>. Text: <visible text in the image, or "none">]
```

Use the same replacement for repeated occurrences of the same image, such as one in the summary and one in the full detail. Keep descriptions brief and factual. Include only text that is visibly present in the image.

This pass applies to the **markdown only**. The raw envelope already carries the real image bytes, so it needs no description pass — and it must not be hand-edited.

If image inspection is not available, do not silently leave the task as complete. Report that the extractor wrote pending image markers and that image descriptions could not be generated in this run.

## Upload

Tell the user to drag **both** files per session into Qualified — the `.md` and the `.json`. The markdown is what gets scored; the raw envelope is what lets a human grader see the images and the agent's actual output. Uploading only one of them loses half the picture.
