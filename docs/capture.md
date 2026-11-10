# Assessment session capture

The capture plugin saves JSON session envelopes to the assessment repository's
`session-logs` branch. Candidates need their coding harness, Git authentication,
and Docker running with Linux containers. They do not need host Python, Compose,
an assessment application stack, or a local image build.

The v1 artifacts must be published and verified before candidates use these
instructions. See [release verification](release.md).

## Install the plugin

Download `session-log-plugins-posix.tar.gz` for macOS or Linux, or
`session-log-plugins-windows.tar.gz` for native Windows. These text bundles work
on both amd64 and arm64 hosts.

Extract the archive into a permanent directory. It contains the three harness
plugins and their shell/PowerShell hooks. Choose the command for your harness,
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
once; it remains inactive in checkouts without `.session-log-capture`.

## Configure an assessment

The assessment maintainer commits `.session-log-capture` at the Git checkout
root:

```text
ghcr.io/woven-reviews/session-log-extractors@sha256:REPLACE_WITH_RELEASE_DIGEST
```

Copy the exact image reference from the release's `capture-image.txt`. During
candidate setup, pull that exact reference with `docker pull`. Hooks use
`--pull=never` and require the image to be present. Capture accepts that
repository's SHA-256 digest, never a moving image tag. Maintainer tests can use
an immutable local `sha256:...` image ID.

The repository's `origin` must identify its GitHub assessment repository. The
hook normalizes `git@github.com:owner/repo.git` to HTTPS for container uploads.
It uses the host's existing Git HTTPS credential helper without interactive
prompts. SSH-only authentication is insufficient: configure HTTPS credentials
outside the coding agent, or supply `SESSION_LOG_TOKEN` in the harness's runtime
environment. Tokens are passed to temporary upload containers, not written into
requests, logs, the image, or the published plugin.

## Resolve readiness errors

Before each supported tool call, the plugin checks Docker availability, the
cached pinned image, and Git HTTPS credential availability. The Stop hook checks
the selected source files before starting extraction. If setup is incomplete,
the hook denies the tool call and gives the agent a reason. The agent explains
the fix in chat; the candidate performs it outside the agent. Conversation
remains available.

Typical candidate operations are starting Docker, checking `docker info`,
pulling the exact configured image with `docker pull`, or signing in through
their Git HTTPS credential helper. From the assessment directory, the candidate
can also run `sh PATH_TO_BUNDLE/claude/hooks/capture.sh doctor claude` (or
`powershell.exe -File PATH_TO_BUNDLE/claude/hooks/capture.ps1 doctor claude` on
Windows). Diagnostics require no host Python. The next tool attempt rechecks
readiness without restarting the session.

This gate uses native harness hooks. It does not classify shell commands or
permit agent-operated repair commands. Native limitations still apply: [Codex's
hook coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage) excludes some
tool paths, and [Copilot hook
timeouts](https://docs.github.com/en/copilot/reference/hooks-reference#exit-codes-for-command-hooks)
fall through to ordinary permissions. A hung Docker engine or credential helper
can also exceed a native hook timeout; verify those programs outside the agent
when diagnostics stall. This is a readiness guard for supported local tools, not
a security boundary against bypassing a harness's own controls.

## Source access and pending work

Containers never receive the host home directory or an ancestor of it. Hooks
reject a checkout or capture-state path that would expose either, including
symlink aliases. Windows also rejects junctions in admitted source paths.

The configured assessment checkout is mounted read-only, along with selected
native source files. Claude and Codex use their selected transcript. Copilot
uses its database, any existing SQLite WAL/SHM files, and the selected session's
`events.jsonl`; `COPILOT_HOME` is supported. No native session directory or home
directory is mounted. The Copilot database can contain other session rows; the
reader selects the exact recorded session ID and checks checkout ownership.

Hook JSON is decoded by a short, network-disabled container using the cached
image. The host reads explicit fields from its output without evaluating shell
code or requiring host Python or jq. Images recorded inline or inside the
assessment checkout remain available. External image files are reported as
unavailable; the hook does not grant additional directory access to find them.
Checkout image symlinks cannot escape the admitted checkout.

The only writable directory mount is the checkout's private Git capture state,
`session-log-capture` beneath its Git directory. It contains requests, pending
work, diagnostics, and an isolated bare upload repository. Deleting it discards
pending captures. Native source files must remain available until extraction
finishes; pending work preserves the request rather than duplicating the entire
native store.

## Capture and recovery behavior

The Stop hook saves its raw signal, checks readiness, and starts a temporary
container with Docker's detached mode. Python stages pending work and performs
extraction and uploads inside that container. The image must already be pulled;
there is no detached host process or background first-pull orchestration. Stop
failures keep chat available and retain saved signals for recovery after setup
is repaired. No permanent capture service is required.

Workers wait for the source to settle and reread it, so the final reply can be
saved without another turn. Requests have distinct filenames. Each worker
handles only requests whose recorded session and exact source paths match its
admitted sources, and a shared lock serializes uploads without stealing a lock
from a slow live worker. A newer turn remains queued while an older upload
finishes. Git fetches the latest `session-logs` parent on every attempt,
including after a conflicting push.

Upload failures retry up to three times and retain pending work for a later Stop
hook for the same session and source paths. Saved signals for other sessions
remain untouched until those sessions run again. A secret-protection rejection
moves the request to `rejected` and does not automatically resend it. Inspect
`last_error` in capture state and the hook/container error output for
diagnostics. Capture never checks out a branch or changes the candidate's index;
its Git operations use its isolated bare repository.

Outputs are `session_logs/HARNESS/SESSION_ID.json` envelopes using
`raw-session-log/1`. Automatic capture explicitly caps textual tool results at
4,096 UTF-8 bytes, preserving the earlier assessment capture policy. Manual
export defaults to 8,192. Inputs, structured facts, conversation, and available
images remain complete under either policy. Capture does not run the fixture
sanitizer or rewrite recorded content.
