---
name: extract-copilot-session-logs
description: Manual-only helper for exporting all GitHub Copilot CLI session logs for a checkout of this repo by running scripts/extract_copilot_session_log.py (via Docker, with --all), then replacing dumped-image markers with brief image descriptions. Writes both a readable markdown log and a raw JSON envelope with images inlined as base64; both get uploaded. Use only when explicitly invoked as $extract-copilot-session-logs or selected from the skills UI.
---

# Extract Copilot Session Logs

Run the Copilot session-log extractor for every Copilot CLI session associated with the current checkout of this repo, then describe any pasted images that were dumped by the extractor. It runs in Docker (see [scripts/Dockerfile](../../../scripts/Dockerfile)) so no local Python install is required — only Docker itself.

1. Resolve the current Git root with `git rev-parse --show-toplevel`. Verify that the root directory contains `scripts/extract_copilot_session_log.py` — this repo's checkouts (the maintainer repo itself, and any candidate clone of it, regardless of naming) all carry that script at their root. If it is not there, stop and report that the current directory is not a checkout of this repo; do not search for or extract a session from another repository.
2. Verify `docker` is available (`command -v docker`). If it is not, stop and tell the user to install Docker (or start Docker Desktop) and try again.
3. Set the command working directory to that verified root and run:

```bash
image="rfp-monster-session-log-extractor:local"
docker build -q -t "$image" scripts >/dev/null
mounts=(-v "$PWD:$PWD" -v /tmp:/tmp)
[ -d "$HOME/.copilot" ] && mounts+=(-v "$HOME/.copilot:$HOME/.copilot:ro")
docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$PWD" "${mounts[@]}" "$image" \
  /extractor/extract_copilot_session_log.py --all
```

The container only gets read-only access to `~/.copilot` (not the user's whole home directory), plus read-write access to this checkout and `/tmp` (where pasted images get dumped) — nothing else on the machine is exposed to it.

4. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--db PATH`, and `--strict` (only export sessions started in this exact directory — no parent/descendant cwd overlap, no newest-anywhere fallback). `--no-raw` skips the raw envelope; `--raw-tool-result-bytes N` changes the tool-result cap (negative for no cap).
5. Record the files written from the command output, then complete the image description pass below before sending the final report.

## What gets written

Two files per session:

| File | What it is |
|---|---|
| `copilot_session_log_<identifier>.md` | The readable turn-by-turn log. Condensed: tool calls become one bullet each, images become a text description. |
| `copilot_session_log_raw_<identifier>.json` | The raw envelope. Full conversation, the tools that ran, and every attached image inlined as base64. |

Copilot records an image as an inline `[image: name]` token and keeps the payload elsewhere — the SQLite `attachments` table, or the per-session events log — so an image only makes it into the envelope if that correlation found something readable. A token with no matching attachment record is recorded as `{"unavailable": true, "ref": ...}` rather than dropped. Copilot's store does not keep tool *output*, so tool calls in the envelope carry a name and target but no result.

## Image Description Pass

The extractor is mechanical and cannot describe images itself. When a pasted image is present and the image file is available, it dumps the image to a temp file and writes this stable marker into the generated markdown:

```markdown
[Image dumped to `<path>` — description pending]
```

After extraction, inspect the generated `copilot_session_log_*.md` files that were just written. For every dumped-image marker line:

1. Extract the image path between the backticks.
2. Inspect that local image with an image-capable viewer/tool.
3. Replace every occurrence of that same marker in the generated markdown with:

```markdown
[Image: <one- or two-sentence description>. Text: <visible text in the image, or "none">]
```

Use the same replacement for repeated occurrences of the same image, such as one in the summary and one in the full detail. Keep descriptions brief and factual. Include only text that is visibly present in the image.

This pass applies to the **markdown only**. The raw envelope already carries the real image bytes, so it needs no description pass — and it must not be hand-edited.

Also scan for unresolved image references, which indicate Copilot recorded an image mention but no readable source file path:

```markdown
[Image referenced as `<name>` — source file unavailable; description pending]
```

and raw inline Copilot tokens:

```markdown
[image: <name>]
```

If either unresolved form is present, do not claim image descriptions are complete. Report exactly which logs still contain unresolved image references and that descriptions could not be generated for those references in this run.

If image inspection is not available, do not silently leave the task as complete. Report that pending image markers were found and image descriptions could not be generated in this run.

## Upload

Tell the user to drag **both** files per session into Qualified — the `.md` and the `.json`. The markdown is what gets scored; the raw envelope is what lets a human grader see the images and the agent's actual output. Uploading only one of them loses half the picture.
