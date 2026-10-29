# Claude Code interactive capture

Launch the resolved `claude` executable in the fresh temporary workspace under tmux, without print mode. Observe local startup/trust/auth screens without changing account credentials. If authentication blocks work, record the failure and stop that phase at the common bound.

## Native controls

- Use actual `AskUserQuestion` activity for structured clarification. Observe offered controls before selecting, multi-selecting, entering custom text, or cancelling. Check native `answers` and annotations; a rendered summary can conceal quote truncation or notes-only input.
- Enter plan mode using the observed control/menu. Obtain a real `ExitPlanMode` review. Distinguish reject-with-feedback, revised approval, and approval after editing in the genuine plan-review editor. Do not assume the editor is available in every version.
- Use real file tools and Bash for disposable work. Set permission behavior through observed project/session controls. Deny harmless actions through the real permission UI; preserve written feedback separately from automatic refusal text.
- Prepare a workspace skill under `.claude/skills/<name>/SKILL.md` and a custom command under `.claude/commands/<name>.md`. Use distinct `CAPTURE_SKILL_*` and `CAPTURE_COMMAND_*` response markers. Verify the actual native `Skill` invocation independently of the slash-command echo and expanded command content.
- Use the actual `!` user-shell interface for the user shell target, separately from assistant Bash calls.
- Request one bounded read-only subagent. Verify the recorded native tool name and IDs rather than assuming it is named `Agent` or `Task` in this version. Collect the child sidecar and wait for completion.
- Use the observed `/model` menu for a different available model and verify the next assistant record's identity. Resume the exact recorded session using the installed CLI's normal resume control/arguments.

## Persistence and attachments

Determine native session paths from this workspace/session's observed metadata. Collect only the selected main transcript and its relevant sidecars/assets, maintaining original relative layout. Preserve parent task activity and child conversation separately. Inspect full tool result blocks, including non-text/tool-reference content, not only string summaries.

For images, use a supported terminal paste/attachment control. Verify an actual user-message image block and persisted media type. A visible attachment indicator is useful UI evidence but insufficient persistence evidence; source GIF/JPEG/WebP may be stored as PNG.

Record the running process's version and resolved executable before launch and again around resume. Auto-updates can change the installed executable while a session process still reports the older version. Record both observations without rewriting history.

These controls reflect observed Claude capture behavior; discover the installed UI rather than hard-coding key sequences. A missing menu/tool is a gap, not permission to substitute prose or headless operation.
