---
name: capture-native-session
description: Capture faithful native Claude Code, Codex, or GitHub Copilot CLI sessions through agent-operated interactive terminals, then sanitize and validate evidence for reviewed extractor fixtures.
---

# Capture native sessions

Run the normal interactive CLI through tmux, choosing individual prompts and navigation from observed screens. This is the standard fixture capture process for Claude, Codex, and Copilot, replacing scripted headless fixture generation. Do not use print/headless mode, an SDK, app-server, or a predetermined conversation script.

Keep one complete main conversation per harness, including later scenarios and exact-session resume. Subagents have their own native sidecars; they do not replace the main conversation. Different tests use recorded IDs to find evidence within this same fixture, rather than splitting it into scenario fixtures.

## Select a harness

Read only the reference for the requested harness before operating it:

- [Claude Code](references/claude.md)
- [Codex](references/codex.md)
- [GitHub Copilot](references/copilot.md)

For an all-harness request, repeat the process separately for each harness. Do not treat a feature or tool with the same name as equivalent across harnesses. Discover supported controls from the installed CLI; unsupported behavior is a reported gap.

## Scope and bounds

- Launch in a fresh temporary workspace outside this repository. Keep the repository, its instructions, tests, fixtures, previous logs, and this operator brief outside the captured session. Do not copy skill instructions into that workspace or conversation.
- Limit captured work to disposable local assets. Do not request unrelated projects, credentials, or external services. Do not change global configuration; use project/session controls and real permission menus.
- Default to at most 40 minutes of interactive operation for the requested capture run, including all selected harnesses. Stop a stalled phase after five minutes and continue to achievable coverage. Prioritize questions, plan decisions, tool results, and export validation over images and optional plan editing.
- Use one main session per harness. Allow at most one fresh retry session per harness for blocked high-priority interactions. Preserve the original evidence and identify the retry separately; never merge session identities.
- Make bounded model calls authorized by the capture request, including at most one real subagent and a short turn on a second available model per harness. Do not start paid capture during tests or merely to validate this skill's instructions.
- Do not change production code, existing fixtures, or generators during capture. Fixture promotion is a separate review step. Do not commit unless requested, and honor explicit artifact exclusions.

## Prepare and operate

1. Read applicable operator instructions outside the captured conversation. Inspect interface documentation only as needed to invoke extraction/validation. Do not inspect this codebase to invent more scenarios.
2. Establish the evidence layout in [Evidence and validation](references/evidence-and-validation.md). Keep raw originals outside the repository. Record UTC start time, resolved executable, CLI version, launch arguments, workspace, observed configuration, and exact native session IDs. Record later version changes, including an auto-updater changing the executable while an older process remains running.
3. Before model work, verify tmux screen capture, literal typing, Enter, and menu navigation. Store timestamped snapshots and an action journal. Capture the screen before an action and verify its effect afterward; waiting or sending a key is not proof of success. Preserve the last screen before clean exit because the pane may disappear.
4. Prepare only local disposable assets: a short text file, a UTF-8 text file larger than 12 KB with repeated multibyte characters, a workspace-local skill, and a custom slash command when supported. Give skill and command distinct response markers. Record exact contents and provenance outside the conversation. Generate harmless local images only if attempting attachments.
5. Work through [Conversation coverage](references/conversation-coverage.md), adapting prompts to observed responses. Use distinctive `CAPTURE_*` markers. A prompt can request a tool interaction, but only actual native tool activity counts. Keep evidence collection, sanitization, extraction, and test auditing outside the captured CLI.
6. Exit cleanly and resume the exact main session with the normal interactive CLI. Send a context-dependent follow-up and verify its response and unchanged native ID. If resumption fails, report that result rather than substituting a new session.
7. Collect only these sessions' native transcripts and relevant sidecars/assets. Preserve original names and relationships. For databases, use a consistent, supported snapshot/export of the selected session records and dependencies; do not alter the live store or collect unrelated sessions.

## Validate and deliver

Follow [Evidence and validation](references/evidence-and-validation.md) to sanitize copies deterministically, interpret through the existing public reader/interpreter interfaces, and validate exports. Use native answer maps, annotations, full result blocks, and recorded IDs as independent evidence; agreement between two paths sharing one parser does not establish faithfulness.

Deliver a concise findings report and coverage matrix with exact IDs, versions, operation duration, failures/retries, and available cost/token usage. Attribute loss to terminal operation, CLI persistence, interpretation, or export. Mark unknown or unmatched evidence explicitly. Include the smallest next step to promote successful material into reviewed fixtures.

Keep the full evidence bundle untracked at `captures/<harness>/<cli-version>/<run-id>/`. For now, promote **one complete sanitized native capture fixture per harness per CLI version** to `scripts/fixtures/<harness>/<cli-version>/`, with its required sidecars and manifest. Do not split it by scenario. Multiple runs can remain in `captures/` without becoming additional committed fixtures.
