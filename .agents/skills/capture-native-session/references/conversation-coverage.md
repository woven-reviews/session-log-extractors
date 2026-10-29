# Conversation coverage

Exercise these targets within one continuing main conversation per harness. Select prompts and UI actions dynamically; this list is coverage, not a transcript to replay. Record each target's outcome even when the installed harness cannot support it.

## Ordinary conversation and recall

Begin with a short user request containing Markdown, Unicode, and a distinctive `CAPTURE_CONTEXT_*` marker. Obtain a response. Later ask for the marker without repeating it. Preserve context through tools, model changes, and resume.

## Native structured clarification

Obtain the harness's genuine structured-question invocation and answer through its terminal UI. Prose questions and typed imitations do not count.

- Answer a single-choice question with an offered option.
- Answer one invocation containing multiple questions, including a multi-select question. Select at least two options and answer every question.
- Answer a separate question using the actual custom-text/Other control, with a distinctive marker matching none of the offered labels.
- If supported, select an option with a Markdown/code preview and check whether preview annotations persist.
- Give a separate custom answer containing a comma, double quotes, and Unicode. Attempt multiline input only if the observed control supports it.
- Cancel an additional question through the actual UI.

After successful answers, ask the assistant to quote them without including their markers in the follow-up. Verify receipt against the native answer maps and annotations, not just the rendered summary. Distinguish offered-option selection from notes-only input; a note does not prove an option was selected. Cancellation must remain separate from an answer.

## Plan lifecycle

Enter genuine plan mode and request a tiny Python greeting program confined to the temporary workspace. Obtain the native plan approval menu.

1. Reject the initial plan through the UI with written `CAPTURE_PLAN_REJECT_*` feedback changing a concrete requirement.
2. Verify that the assistant receives the feedback and presents a revised plan implementing that requirement.
3. Approve the revision through the real UI, preferably preserving context and requiring manual edit approval. Permit only the small approved implementation.
4. If time and the observed UI permit, obtain another plan and edit it through the genuine plan-review editor, then approve the edited version. Record the proposed and edited text plus native approval evidence. Reject-and-revise is not edited approval.

Record unsupported approval/editing workflows as skipped. A plan written in chat is not evidence of plan mode; selecting a menu item is not proof of persisted approval.

## Tools, errors, repeated calls, and permission decisions

- Write a small source file, read it, edit it, and read it again. Use distinct before/after markers to check that repeated calls retain their own results.
- Make one real file-write or edit tool input exceed 4 KB. Check the complete recorded input separately from result truncation.
- Read the prepared multibyte UTF-8 file, obtaining at least one recorded textual tool result exceeding 10 KB. A large file alone does not establish a large persisted result.
- Run a harmless successful shell command producing a recognizable marker.
- Run a harmless command exiting nonzero with distinctive stderr.
- Read a deliberately missing workspace file to obtain a real tool error.
- Trigger a genuine permission prompt for a harmless local action. Deny it with written `CAPTURE_PERMISSION_DENY_*` feedback if supported and verify the assistant receives the refusal. Attempt another denial without feedback if readily available.
- Enter a harmless user shell command through the CLI's actual user-shell interface when available. Distinguish it from an assistant shell tool call.

Use scoped interactive permission controls. Do not provoke prompts with destructive commands or broaden workspace access. A settings-generated refusal is not a user denial. An attempted tool action is not evidence of successful completion.

## Skills, commands, and a subagent

Invoke the prepared workspace skill through the actual native skill mechanism, and the custom slash command directly through the terminal. Verify the distinct response markers and native invocation evidence separately. Slash-command echo or skill-like prose does not establish tool use.

Have the assistant launch one real subagent for a bounded read-only task, such as inspecting the disposable greeting program. Give task and result a unique marker. Wait for completion, collect its related native records, and distinguish the main session's task invocation/summary from the child's own conversation. If the harness lacks this facility, report it as unavailable.

## Model change and resume

Use the observed model menu to select another available model. Send a short prompt requiring recall of the earlier marker, then verify both its response and recorded assistant model identity. Menu selection alone is insufficient. Do not change account settings or use a different provider merely to fill this target.

Exit and resume the exact same main session using normal interactive CLI controls. Send another context-dependent follow-up. Record session identity before and after; repeated process launches need not mean separate conversations.

## Images, when practical

Use harmless locally generated images and a genuine supported terminal attachment/paste workflow. Attach two distinguishable images in one user message and another image in a later message; ask the assistant to distinguish them.

If practical, try PNG, JPEG, GIF, and WebP. Record source formats separately from persisted media types: the clipboard/UI may convert everything to PNG. A filename in prose, file-read tool call, or inserted JSON is not evidence of a user-message image attachment. Stop after the phase bound if genuine attachment cannot be achieved.
