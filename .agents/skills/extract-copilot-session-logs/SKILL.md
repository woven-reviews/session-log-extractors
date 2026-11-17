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
git_common=$(git rev-parse --path-format=absolute --git-common-dir)
mounts+=(-v "$git_common:$git_common:ro")
git_safe_count=1
git_env=(-e GIT_CONFIG_KEY_0=safe.directory -e "GIT_CONFIG_VALUE_0=$PWD")
while IFS= read -r -d '' field; do
  case "$field" in
    'worktree '*)
      worktree_path=${field#worktree }
      if [ -d "$worktree_path" ] && [ "$worktree_path" != "$PWD" ]; then
        mounts+=(-v "$worktree_path:$worktree_path:ro")
        git_env+=(-e "GIT_CONFIG_KEY_$git_safe_count=safe.directory" -e "GIT_CONFIG_VALUE_$git_safe_count=$worktree_path")
        git_safe_count=$((git_safe_count + 1))
      fi
      ;;
  esac
done < <(git worktree list --porcelain -z)
git_env+=(-e "GIT_CONFIG_COUNT=$git_safe_count")
[ -d "$HOME/.copilot" ] && mounts+=(-v "$HOME/.copilot:$HOME/.copilot:ro")
docker run --rm -e HOME="$HOME" -u "$(id -u):$(id -g)" -w "$PWD" "${git_env[@]}" "${mounts[@]}" "$image" \
  "$PWD/scripts/extract_copilot_session_log.py" --all --output "$PWD"
```

The container only gets read-only access to `~/.copilot` (not the user's whole home directory), read-only access to `/tmp` for referenced attachments, read-only access to registered linked worktrees and the shared Git directory, and read-write access to this checkout.

The command runs the verified checkout's script, whose project root is the checkout. `--output "$PWD"` also sets the destination explicitly.

4. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--state-root DIR`, and `--strict` (only export sessions started in this exact directory). `--tool-result-bytes N` changes the tool-result cap (negative for no cap).
5. Record the files written from the command's stderr diagnostics and report any skipped sessions or warnings.

## Output destinations

Keep output inside the shared checkout. For batch export, `--output ./exports`
selects a directory beneath the checkout; destinations outside the checkout are
unsupported by this Docker workflow. The helper above always uses `--all`;
a positional ID appended to it does not switch to single-session extraction.
To extract one session, invoke the same mounted script with a session selector
and omit `--all`. In that mode, `--output ./exports/session.json` names a file,
while `--output -` writes stdout. Replace any batch `--output` argument too;
do not pass a batch directory as a single-session file destination.

The default `--all` selection covers the main checkout and registered linked
worktrees, including subdirectories. It never falls back to ancestor directories
or unrelated repositories.

## What gets written

One `copilot_session_log_raw_<identifier>.json` file per session. Each envelope contains session metadata, ordered messages, tool calls with full inputs, and results capped at the configured byte limit with original size and truncation metadata.

Copilot records images in the per-session events log. Available image bytes are inlined as base64. A reference without a readable source remains explicit as `{"unavailable": true, "ref": ...}`. Do not hand-edit the generated JSON or add image descriptions.

## Upload

Tell the user to drag each generated `.json` file into Qualified. Report skipped sessions and unavailable image references without claiming those images were recovered.
