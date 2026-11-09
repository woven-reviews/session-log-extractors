# Assessment session capture

The capture plugin saves JSON session envelopes to the assessment repository's
`session-logs` branch. Candidates need their coding harness, Git authentication,
and Docker running with Linux containers. They do not need Python, Go, Compose,
an assessment application stack, or a local image build.

The v1 artifacts must be published and verified before candidates use these
instructions. See [release verification](release.md).

## Install the plugin

Download the published `session-log-plugins-OS-ARCH.tar.gz` asset for the host:

| Host | OS | ARCH |
| --- | --- | --- |
| Apple Silicon Mac | darwin | arm64 |
| Intel Mac | darwin | amd64 |
| Linux | linux | amd64 or arm64 |
| Native Windows | windows | amd64 or arm64 |

Extract the archive into a permanent directory. It contains the three harness
plugins and their prebuilt host executables. Choose the command for your harness,
replacing `PATH_TO_BUNDLE` with the extracted directory's absolute path:

```sh
codex plugin marketplace add "PATH_TO_BUNDLE"
codex plugin add session-log-capture@woven-session-logs
```

```sh
claude plugin marketplace add "PATH_TO_BUNDLE"
claude plugin install session-log-capture@woven-session-logs
```

```sh
copilot plugin install "PATH_TO_BUNDLE/copilot"
```

Follow the harness's normal plugin and hook trust prompts. Install the plugin
once; it remains inactive in checkouts without `.session-log.json`.

## Configure an assessment

The assessment maintainer commits `.session-log.json` at the Git checkout root:

```json
{
  "version": 1,
  "image": "ghcr.io/woven-reviews/session-log-extractors@sha256:REPLACE_WITH_RELEASE_DIGEST"
}
```

Copy the exact image reference from the release's `capture-image.txt`. Capture
accepts that repository's SHA-256 digest, never a moving image tag. Maintainer
tests can use an immutable local `sha256:...` image ID.

The repository's `origin` must identify its GitHub assessment repository. The
launcher normalizes `git@github.com:owner/repo.git` to HTTPS for container uploads.
It uses the host's existing Git HTTPS credential helper without interactive
prompts. SSH-only authentication is insufficient: configure HTTPS credentials
outside the coding agent, or supply `SESSION_LOG_TOKEN` in the harness's runtime
environment. Tokens are passed to temporary upload containers, not written into
requests, logs, the image, or the published plugin.

## Resolve readiness errors

Before each supported tool call, the plugin checks the selected source files,
Docker availability, the pinned image, and Git HTTPS credential availability.
If setup is incomplete, the hook denies the tool call and gives the agent a
reason. The agent explains the fix in chat; the candidate performs it outside
the agent. Conversation remains available.

Typical candidate operations are starting Docker, checking `docker info`,
pulling the exact configured image with `docker pull`, or signing in through
their Git HTTPS credential helper. From the assessment directory, the candidate
can also run the bundle's `claude/bin/session-log-hook.exe doctor` (the identical
executable is included in each harness folder). Diagnostics require no Python.
The next tool attempt rechecks readiness without restarting the session.

This gate uses native harness hooks. It does not classify shell commands or
permit agent-operated repair commands. Native limitations still apply:
[Codex's hook coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage)
excludes some tool paths, and
[Copilot hook timeouts](https://docs.github.com/en/copilot/reference/hooks-reference#exit-codes-for-command-hooks)
fall through to ordinary permissions. The local readiness checks have short
deadlines so normal setup failures produce an explicit denial before the hook
timeout. This is a readiness guard for supported local tools, not a security
boundary against bypassing a harness's own controls.

## Source access and pending work

Containers never receive the host home directory, a parent of it, or the entire
assessment checkout. Source admission rejects directory mounts and symlinks
that resolve to the home directory or its ancestors.

The host exposes individual session files read-only. Claude and Codex use their
selected transcript. Copilot uses its database, any existing SQLite WAL/SHM
files, and the selected session's `events.jsonl`; `COPILOT_HOME` is supported.
A short container with networking disabled lets the native reader identify
recorded image paths after checking session ownership. The host then exposes
the available image files individually for extraction. Missing or inaccessible
images remain explicit unavailable references in the envelope.

The only writable directory mount is the checkout's private Git capture state,
`session-log-capture` beneath its Git directory. It contains requests, pending
work, diagnostics, and an isolated bare upload repository. Deleting it discards
pending captures. Native source files must remain available until extraction
finishes; pending work preserves the request rather than duplicating the entire
native store.

## Capture and recovery behavior

The Stop hook persists a request and hands off to a detached host process. That
process can wait for a slow image pull after the hook has returned. Extraction
and Git upload run in temporary containers, which exit when their work is done.
No permanent capture service is required.

Workers wait for the source to settle and reread it, so the final reply can be
saved without another turn. Requests have distinct filenames. Each worker
handles only its admitted request, and a shared lock serializes uploads without
stealing a lock from a slow live worker. A newer turn remains queued while an
older upload finishes. Git fetches the latest `session-logs` parent on every
attempt, including after a conflicting push.

Upload failures retry up to three times and retain pending work for a later
Stop hook. A secret-protection rejection moves the request to `rejected` and
does not automatically resend it. Inspect `last_error` and `launcher.log` in
capture state for diagnostics. Capture never checks out a branch or changes
the candidate's index; its Git operations use its isolated bare repository.

Outputs are `session_logs/HARNESS/SESSION_ID.json` envelopes using
`raw-session-log/1`. Automatic capture explicitly caps textual tool results at
4,096 UTF-8 bytes, preserving the earlier assessment capture policy. Manual
export defaults to 8,192. Inputs, structured facts, conversation, and available
images remain complete under either policy. Capture does not run the fixture
sanitizer or rewrite recorded content.
