# Codex interactive capture

Launch the resolved normal interactive `codex` CLI in the fresh temporary workspace under tmux. Do not use `exec`, an SDK, app-server, or another noninteractive transport. Keep trust, approval, and sandbox choices scoped to this capture rather than editing global configuration.

Discover the installed CLI's help and visible menus. Do not transfer Claude key sequences, tool names, or persistence assumptions to Codex.

## Native controls

- Find the genuine plan/structured-question workflow. If the available mode supports structured user questions, exercise its actual UI and preserve native request/response records. Plain chat questions do not satisfy this target. If multi-select, previews, cancellation, or custom-text controls are absent, report each limitation.
- Distinguish plan text from a real approval/rejection lifecycle. Request the tiny greeting program through the available plan workflow. Attempt written rejection and revised approval only through supported controls; mark genuine edited-plan approval unavailable if there is no editor workflow.
- Use actual file/shell tools for the common local work targets. Capture command output, exit status, errors, and repeated call/result IDs. A patch-based edit can satisfy file editing; do not relabel it as a Claude Write/Edit tool.
- Use an actual scoped permission prompt for a harmless action. Preserve the recorded decision and any supported feedback. Sandbox/settings failures alone do not prove a user denial.
- Prepare a workspace-local skill in the installed CLI's supported discovery location and invoke its native skill mechanism. Verify persisted invocation versus expanded instructions. Discover whether custom slash commands and a user-shell interface are supported; do not substitute skill invocation or an assistant command for unavailable controls.
- Request one real read-only subagent only if supported. Keep its native identity, task activity, and child records separate from the main conversation.
- Use the observed model menu for a second available model. Require a context-dependent response and recorded model identity. Exit and resume the exact native main session through the installed interactive resume controls.

## Persistence and attachments

Locate only this session's native rollout and related child/attachment records using observed identities and workspace metadata. Keep original filenames/layout; native rollout names need not be the session ID. Do not collect an entire global sessions directory.

Distinguish actual conversation messages/tool calls from replay or diagnostic payloads that embed previous calls. Verify result ownership using recorded call/message IDs and preserve unmatched records explicitly.

Attempt image attachment only through a genuine supported terminal workflow. Preserve any referenced local image assets needed by the existing reader, along with their relation to native messages. Record original and persisted media types separately. Missing assets remain explicit; never reconstruct an image from an assistant description.

This reference defines what to verify, not a claim that all targets are supported by every Codex version. Record observed controls and unavailable features in coverage.


## Observed with CLI 0.166.0 (2026-10-30)

The selected capture in `scripts/fixtures/codex/0.166.0/` used the normal TUI
under tmux. These are observations for that version, not a script to replay.

- Isolate configuration as well as cwd. A fresh workspace alone still inherited
  the operator's global AGENTS instructions. The first attempt was stopped and
  retained privately as failed. The one retry used a disposable `CODEX_HOME`,
  with only an `auth.json` symlink to the existing login. Never copy that home or
  authentication link into evidence. User-level skill discovery can still list
  global skills; check the actual native context for repository/operator leakage.
  `--ignore-user-config` was rejected by this installed interactive CLI.
- Launch used `--no-daemon --no-alt-screen -m gpt-6.1-sol -s workspace-write
  -a on-request -c model_reasoning_effort=low`. Record the exact observed version
  for every launch, and preserve tmux snapshots before exiting.
- `/plan` enters Plan mode. Shift-Tab returned to Default mode; typing `/plan`
  again did not leave Plan mode. Question arrows select options, Enter advances
  and submits, and Tab opens notes. A selection plus note and “None of the above”
  plus custom Unicode/punctuation text both persisted in native answer arrays.
  Escape canceled a question and interrupted its turn. Multi-select and previews
  were absent from the available question schema/UI. A notes-only submission
  without any selection was not established; do not count a custom selection as
  notes-only evidence.
- The plan menu offered implement, clear-context-and-implement, and stay in Plan
  mode. Select stay-in-Plan, then type concrete feedback to reject/revise. Ordinary
  implementation approval persisted as the next `Implement the plan.` input.
  No edited-plan editor was exposed. Avoid the clear-context option when testing
  exact-session continuity.
- An explicitly escalated harmless `printf` produced a real permission menu.
  Selecting its rejection option canceled the request and produced `aborted by
  user`; later feedback was a separate human input, not embedded approval data.
  A separate resume with `-s read-only -a never`, followed by Default mode, produced
  an actual apply-patch refusal from settings. Restore `workspace-write` and
  `on-request` on the next resume. Do not label that refusal as a human denial.
- Native local skill selection used `$capture-greeting`; completion needed an
  additional Enter before submission. Two invocations persisted as separate
  user inputs and `<skill>` instruction expansions with distinct message/turn
  IDs and `skills.selected_skill_instructions` metadata. They were not tool calls.
  A disposable `prompts/capture-echo.md` in the isolated home was not recognized
  by `/prompts:capture-echo ...`; custom-command arguments remain unavailable.
- `!` ran a user shell command and recorded `shell.user_command` provenance,
  a `<user_shell_command>` message, and a `CommandExecution` item with
  `source: user_shell`. Both stdout/stderr markers persisted in combined output;
  separate stream provenance was not recorded. An assistant shell call is a
  different interaction.
- Enter during a running turn queued input. `/stop` stopped a background terminal;
  the subsequent status call returned the original process ID as unavailable.
  A stopped background process did not create a fabricated unmatched result.
- `/model` exposed available models; the `s` shortcut in the effort menu applied
  a model only to this session. A short second-model recall turn was verified in
  `turn_context.model`, then the primary model was restored.
- `codex resume SESSION_ID PROMPT ... -i IMAGE_A -i IMAGE_B` attached images to a
  later input in the same session. Put positional arguments before the variadic
  image arguments: putting them after `-i` opened the resume picker instead.
  PNG, JPEG, GIF, and WebP were accepted; GIF persisted as PNG, while the other
  formats retained their media types. Preserve both source GIF and native PNG
  bytes, and distinguish image tool results from attachments.
- `/compact` completed and retained context recall. Keep private before/after
  source snapshots: this capture appended a `compacted` record with replacement
  history without deleting the original response records. Replacement history
  is replay context, not additional conversation activity.

### Native interpretation boundaries

Code mode recorded `exec` JavaScript inputs and complete structured result blocks,
while `FileChange` items recorded file contents/diffs and exact long paths.
Do not parse JavaScript strings into invented nested tool calls. Opaque `exec`,
`spawn_agent`, `wait_agent`, and `wait` inputs remain explicitly unsupported schemas.
The first large-file write used a short generated expression; a later literal
6.7 KB request was needed to establish complete large-input retention.

A child rollout's first `session_meta.id` is its identity;
`session_meta.session_id` can still name its parent. Use recorded
`parent_thread_id` and `subagent_history_start_ordinal` to identify the relationship
and exclude inherited parent history from the child's own activity. Preserve the
complete native sidecar even when interpreting only its own recorded boundary.
There is no dedicated Codex child-summary API; validate its public reader,
interpreter, and export separately from the parent. The parent’s incoming
`response_item.agent_message` delivery record has no normalized model record;
retain it in the native rollout and report that interpretation gap separately
from the child’s own final response.

Human text with `user.text` provenance must retain literal harness-like XML.
Recorded skill expansions belong to their own native message/turn IDs. Native
answer arrays select whole labels; note text and substring overlap must not
invent additional selections. Export intentionally omits model-only identities,
skill-invocation details, file-change details, and child relationships.
