# Conversation coverage

Exercise these targets within one continuing main conversation per harness. Select prompts and UI actions dynamically; this list is coverage, not a transcript to replay. Record each target's outcome even when the installed harness cannot support it.

## Ordinary conversation and recall

Begin with a short user request containing Markdown, Unicode, and a distinctive `CAPTURE_CONTEXT_*` marker. Obtain a response. Later ask for the marker without repeating it. Preserve context through tools, model changes, and resume.

Include a literal XML example inside a fenced code block in an ordinary human message, such as `<bash-stdout>CAPTURE_LITERAL_MARKUP</bash-stdout>`. Treat it as sample text, not a command or actual shell output. Verify that the exact human-authored content survives native persistence, interpretation and export; distinguish literal markup from harness-injected wrappers using the recorded input and native evidence.

## Native structured clarification

Obtain the harness's genuine structured-question invocation and answer through its terminal UI. Prose questions and typed imitations do not count.

- Answer a single-choice question with an offered option.
- Answer one invocation containing multiple questions, including a multi-select question. Select at least two options and answer every question.
- Answer a separate question using the actual custom-text/Other control, with a distinctive marker matching none of the offered labels.
- If supported, select an option with a Markdown/code preview and check whether preview annotations persist.
- Give a separate custom answer containing a comma, double quotes, and Unicode. Attempt multiline input only if the observed control supports it.
- If the UI supports notes, attempt both a notes-only submission and an offered selection with an additional note as distinct interactions. Use different markers for the note and option; verify native selections, free text and annotations separately, including any UI refusal to submit notes without a selection.
- Offer overlapping labels such as `Leaf` and `Leaflet` in a single-choice question and select only the longer label. Compare the native answer with interpreted chosen labels; a substring match must not invent a second selection.
- Cancel an additional question through the actual UI.

After successful answers, ask the assistant to quote them without including their markers in the follow-up. Verify receipt against the native answer maps and annotations, not just the rendered summary. Distinguish offered-option selection from notes-only input; a note does not prove an option was selected. Cancellation must remain separate from an answer.

## Plan lifecycle

Enter genuine plan mode and request a tiny Python greeting program confined to the temporary workspace. Obtain the native plan approval menu.

1. Reject the initial plan through the UI with written `CAPTURE_PLAN_REJECT_*` feedback changing a concrete requirement.
2. Verify that the assistant receives the feedback and presents a revised plan implementing that requirement.
3. Approve the revision through the real UI, preferably preserving context and requiring manual edit approval. Permit only the small approved implementation.
4. Obtain another plan and attempt editing through the genuine plan-review editor, then approve the edited version. Change a concrete requirement using a distinctive `CAPTURE_PLAN_EDIT_*` marker. Record the proposed text, actual editor change, approved text, and native approval evidence. Reject-and-revise is not edited approval, and an approval payload labeled "edited" alone does not prove that the human used the editor.

Discover the editor control and attempt the supported workflow within the phase bound; do not omit it merely because approval/rejection already exercises the parser. Record unavailable controls, editor-launch failures, and time limits separately. A plan written in chat is not evidence of plan mode; selecting a menu item is not proof of persisted approval.

## Tools, errors, repeated calls, and permission decisions

- Write a small source file, read it, edit it, and read it again. Use distinct before/after markers to check that repeated calls retain their own results.
- Use the prepared long path containing spaces and Unicode in the file-tool sequence. Verify exact complete paths in inputs and file-change records independently of shortened display descriptors. Retain the generated long relative portion during sanitization so path replacements do not erase the boundary under test.
- Request independent native reads of the two short files together, using distinct content markers. Verify whether calls are batched or concurrent, their recorded call/result IDs and actual completion order. Do not claim concurrency from a prompt or manufacture an out-of-order result; report serialized execution when observed.
- If the harness exposes a native notebook-editing tool, edit one cell in a minimal disposable notebook using a `CAPTURE_NOTEBOOK_*` marker. Preserve the before/after notebook assets and actual tool name, path/cell fields, complete input and result. Verify the cell change without executing notebook code. A shell rewrite or generic text edit does not establish native notebook-tool coverage; record unavailable tools explicitly.
- Make one real file-write or edit tool input exceed 4 KB. Check the complete recorded input separately from result truncation.
- Read the prepared multibyte UTF-8 file, obtaining at least one recorded textual tool result exceeding 10 KB. A large file alone does not establish a large persisted result.
- Run a harmless successful shell command producing a recognizable marker.
- Run a successful command that emits neither stdout nor stderr, and another successful command that emits distinct markers to both streams. Verify exit status, recorded stdout/stderr and complete result blocks. An empty process stream does not necessarily mean the CLI persists an empty result; record the actual shape.
- Run a harmless command exiting nonzero with distinctive stderr.
- Read a deliberately missing workspace file to obtain a real tool error.
- Read one prepared image through a native file tool if supported. Preserve the actual text/image or other structured result blocks and their association with the call. Check full interpreted result retention separately from textual export. This tool result is a separate target from a user-message image attachment.
- Invoke an available native local search tool twice with distinct inputs and distinguishable matches in the disposable files. Preserve its actual name, complete inputs/results and IDs. During validation outside the captured conversation, check schema recognition, unsupported-tool reporting and diagnostic deduplication. Do not install a plugin or access an external service to satisfy this target.
- Trigger a genuine permission prompt for a harmless local action. Deny it with written `CAPTURE_PERMISSION_DENY_*` feedback if supported and verify the assistant receives the refusal. Attempt another denial without feedback if readily available.
- Separately configure a narrow session/project deny rule or supported automatic-refusal mode for a harmless local action, then request that action. Verify the actual native call/refusal and distinguish it from a human menu denial or ordinary tool error. Record the effective configuration and restore the capture's previous session/project permission behavior afterward. If the harness blocks the request without persisting a call or refusal, record that limitation rather than claiming tool-result coverage.
- Enter a harmless successful user shell command through the CLI's actual user-shell interface when available, emitting distinct `CAPTURE_USER_STDOUT_*` and `CAPTURE_USER_STDERR_*` markers to stdout and stderr. Verify both persisted streams and their interpretation/export separately from the input command. Distinguish this interaction from an assistant shell tool call; mixed-stream assistant output does not satisfy the user-shell target.

Use scoped interactive controls or capture-workspace settings; never change global permission configuration. Do not provoke prompts with destructive commands or broaden workspace access. A settings-generated refusal is not a user denial. An attempted tool action is not evidence of successful completion.

## Interruption and continuation

Separately attempt queued input: while a harmless bounded tool is visibly running, submit a distinct follow-up through the supported input UI without interrupting it. Record whether the CLI queues, immediately applies, rejects or ignores the input. Verify the eventual native message order and call/result ownership; do not infer an intervening human message from when text was typed.

Attempt both a running-tool interruption and cancellation of a pending permission or plan approval through the harness's observed interactive controls. For the running tool, use a harmless local operation with a short bounded duration; verify that it has started before interrupting it. For pending approval, verify the menu is actually waiting before cancelling. Keep these within the existing main session and phase bounds; do not crash the CLI or corrupt its transcript to force missing records.

After each interruption, send a distinct `CAPTURE_CONTINUE_*` follow-up and verify that the session continues. Preserve native call IDs, any cancellation/error/result records, and the subsequent message/tool IDs. Record whether the interrupted call has a result or remains unanswered, whether later results retain their recorded owners, and how interpretation/export handle that evidence. Do not assume interruption creates an unmatched result or missing-result branch; report the actual persisted outcome, including controls that merely close a menu or exit the process.

## Skills, commands, and a subagent

Invoke the prepared workspace skill through the actual native skill mechanism, and the custom slash command directly through the terminal. Verify the distinct response markers and native invocation evidence separately. Slash-command echo or skill-like prose does not establish tool use.

Define the disposable custom command to expose supplied arguments using the installed harness's supported mechanism. Invoke it with nonempty distinctive arguments containing spaces, punctuation, and Unicode. Preserve the exact typed invocation, command definition, native argument fields and expanded text, and the assistant's response. Verify argument retention through interpretation/export where supported; an assistant echo alone does not establish persisted command arguments. Record unavailable argument controls or absent native fields explicitly.

Invoke the same workspace skill again later in the main conversation. Verify two distinct native invocations with their recorded IDs and results; repeated marker text or reused skill instructions alone do not establish a second invocation. Check that interpretation preserves both calls while deduplicating the session's skill list. If the CLI suppresses a second invocation, preserve that evidence and report the gap instead of manufacturing another call.

Have the assistant launch one real subagent for a bounded read-only task, such as inspecting the disposable greeting program. Give task and result a unique marker. Wait for completion, collect its related native records, and distinguish the main session's task invocation/summary from the child's own conversation. If the harness lacks this facility, report it as unavailable.

## Model change and resume

Use the observed model menu to select another available model. Send a short prompt requiring recall of the earlier marker, then verify both its response and recorded assistant model identity. Menu selection alone is insufficient. Do not change account settings or use a different provider merely to fill this target.

Exit and resume the exact same main session using normal interactive CLI controls. Send another context-dependent follow-up. Record session identity before and after; repeated process launches need not mean separate conversations.

## Manual compaction and recall

If exposed by the installed CLI, invoke manual compaction once through its native control, then ask for an earlier `CAPTURE_CONTEXT_*` marker without repeating its value. Record the control, completion/failure, unchanged session identity and actual recall response. Do not inflate the conversation to force automatic compaction.

Preserve private raw snapshots of this session's native source before and after compaction. Identify retained or rewritten history, summary/boundary records and replayed context. Check interpretation/export against the actual persisted history, including whether historical calls are duplicated or summaries become apparent human turns. Never splice snapshots into a composite transcript or count replayed calls as new tool activity; report unavailable history or controls as gaps.

## Image attachments

Use harmless locally generated images and a genuine supported terminal attachment/paste workflow. Attach two distinguishable images in one user message and another image in a later message; ask the assistant to distinguish them.

Attempt PNG, JPEG, GIF, and WebP through supported attachment controls within the phase bound. Record each source format separately from the persisted media type: the clipboard/UI may convert everything to PNG. Verify native image blocks/assets, their order and owning message IDs, and retention through interpretation and export. A filename in prose, file-read tool call, or inserted JSON is not evidence of a user-message image attachment. If genuine attachment cannot be achieved, preserve the attempted control and observed failure; distinguish unsupported formats from unavailable attachment controls and time limits.
