# Claude Code interactive capture

Launch the resolved `claude` executable in the fresh temporary workspace under tmux, without print mode. Observe local startup/trust/auth screens without changing account credentials. If authentication blocks work, record the failure and stop that phase at the common bound.

Check for transcript-write failures before the first model turn. If the operator sandbox blocks persistence or normal credential refresh, correct the launch permissions before spending the fresh-session retry; do not treat that failure as a reason to change credentials.

Disable account-synced skills and plugins with session-only settings on every launch, including retries and exact-session resume:

```sh
claude --settings '{"syncClaudeAiSkills":false,"syncClaudeAiPlugins":false}'
claude --settings '{"syncClaudeAiSkills":false,"syncClaudeAiPlugins":false}' --resume <native-session-id>
```

These settings stop both syncing and loading previously synced material while keeping the disposable workspace skill and custom command available. Do not change global settings. Check that the installed version supports these settings; if it cannot honor them, report the isolation gap before model work. Use a fresh session for a new capture: disabling sync does not remove skill descriptions already recorded in an existing conversation.

Before the first model turn, inspect `/skills` or `/context` for unexpected organizational skills and record the check outside the captured conversation. Verify the persisted skill listing in the main transcript and child sidecars during collection, since skill names and descriptions can be recorded without invoking their bodies. If organizational material appears, stop that capture and report the gap; retain raw evidence privately. These overrides isolate account-synced material, so inspect other installed skills and plugins separately rather than assuming the entire environment is isolated. Record the exact settings with each launch in `run.json`.

## Native controls

- Use actual `AskUserQuestion` activity for structured clarification. Observe offered controls before selecting, multi-selecting, entering custom text, or cancelling. Attempt notes-only and selection-plus-note submissions when supported, and a single-choice question with overlapping labels selecting only the longer one. Check native `answers` and annotations; a rendered summary can conceal quote truncation or notes-only input.
- Enter plan mode using the observed control/menu. Obtain a real `ExitPlanMode` review. Exercise reject-with-feedback and revised approval, then attempt another approval after editing in the genuine plan-review editor. For tmux operation, set a terminal editor through launch-local `EDITOR`/`VISUAL` values (for example, `EDITOR=vim VISUAL=vim`) before launch/resume. Discover the editor control in the installed version and verify the human's edit against the native approved plan; do not count an echoed plan or assistant revision as editor use.
- Use real file tools and Bash for disposable work. Set permission behavior through observed project/session controls. Deny harmless actions through the real permission UI; preserve written feedback separately from automatic refusal text. Separately attempt a settings-driven denial using a narrow capture-workspace deny rule or supported session mode, verify its native refusal, and restore the prior capture permission behavior. Do not relabel it as a user denial.
- Discover whether the installed tool set exposes `NotebookEdit`. If available, edit a cell in the prepared disposable notebook and verify the native notebook path/cell payload, result, and actual file change. Do not substitute Bash or a generic Edit call for notebook-tool evidence.
- Exercise the shared long-path, independent-read, empty-success and stdout/stderr targets through actual file/Bash tools. Attempt queued follow-up input during a bounded running tool and verify native ordering. If local search tools such as Glob or Grep are exposed, invoke one twice against disposable assets; verify actual calls before checking extractor schema support outside the captured conversation.
- Prepare a workspace skill under `.claude/skills/<name>/SKILL.md` and a custom command under `.claude/commands/<name>.md`. Use distinct `CAPTURE_SKILL_*` and `CAPTURE_COMMAND_*` response markers. Invoke the same local skill twice in the main conversation and verify distinct actual native `Skill` calls/results independently of slash-command echoes, expanded command content, or repeated response markers.
- Invoke the local command with nonempty arguments containing spaces, punctuation, and Unicode. Verify its supported argument expansion and the native `<command-args>` content when present, then check shared `command_args` and export behavior against the native evidence. Missing native arguments and interpretation/export omissions are separate gaps.
- Use the observed interrupt/cancel controls once during a harmless bounded running tool and once at a pending permission or plan approval. Verify each active state before acting, then send a follow-up in the same session. Collect the actual cancellation/result or unanswered-call evidence and subsequent IDs; do not infer the persisted shape from the terminal alone.
- Use the actual `!` user-shell interface for a harmless successful command emitting distinct stdout and stderr markers, separately from assistant Bash calls. Verify the native input and both output streams, then check their retention through interpretation/export.
- Request one bounded read-only subagent. Verify the recorded native tool name and IDs rather than assuming it is named `Agent` or `Task` in this version. Collect the child sidecar and wait for completion.
- Use the observed `/model` menu for a different available model, choosing its session-only control rather than changing the default, and verify the next assistant record's identity. Resume the exact recorded session using the installed CLI's normal resume control/arguments.
- Discover the installed manual compaction control, such as `/compact`, and attempt it once with subsequent context-dependent recall. Preserve pre/post native-source snapshots privately and inspect actual summary/boundary records without merging source versions or replaying historical activity.

## Persistence and attachments

Determine native session paths from this workspace/session's observed metadata. Collect only the selected main transcript and its relevant sidecars/assets, maintaining original relative layout. Preserve parent task activity and child conversation separately. Inspect full tool result blocks, including non-text/tool-reference content, not only string summaries.

Attempt images through a supported terminal paste/attachment control: two distinguishable images in one user message and another in a later turn. Attempt PNG, JPEG, GIF, and WebP as supported, recording source and persisted media types separately. Verify actual user-message image blocks, their recorded owners and order, and their retention through interpretation/export. A visible attachment indicator is useful UI evidence but insufficient persistence evidence; source GIF/JPEG/WebP may be stored as PNG. Record observed failures or unsupported controls individually within the phase bound.

Separately request a native Read of a prepared image if supported. Inspect the actual nested result blocks and retain their call ID and full payload through interpretation; textual export may omit non-text content. Do not confuse a tool-returned image with a user attachment or a filename mentioned in prose.

Record the running process's version and resolved executable before launch and again around resume. Auto-updates can change the installed executable while a session process still reports the older version. Record both observations without rewriting history.

These controls reflect observed Claude capture behavior; discover the installed UI rather than hard-coding key sequences. A missing menu/tool is a gap, not permission to substitute prose or headless operation.
