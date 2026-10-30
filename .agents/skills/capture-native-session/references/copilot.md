# GitHub Copilot interactive capture

Launch the resolved normal interactive `copilot` CLI in the fresh temporary workspace under tmux. Do not use prompt/print mode, an SDK, or a scripted exchange. Observe the installed CLI's help, trust controls, and menus rather than assuming Claude or Codex commands work here.

## Native controls

- Discover the actual structured-question UI and native request/answer records, if available. Exercise each supported selection/custom-text/cancellation variant. Chat prose, a slash-command echo, or a proposed question is insufficient evidence.
- Discover plan mode and its actual review controls. Distinguish a plan displayed as text, a native decision, written rejection/revision, and a genuine editor-based approval. Report unavailable workflows individually.
- Use actual file and shell tools for disposable greeting work, large input/result targets, nonzero exit, and missing-file errors. Keep native tool naming and recorded result structure; do not translate them into another harness's tool schema.
- Trigger a harmless action under scoped permission controls and deny it through the actual menu. Verify native refusal evidence and receipt of written feedback if supported. Keep user denial separate from configured automatic refusal.
- Create workspace skill/custom-command assets only in locations supported by the installed version. Verify native invocation and response markers independently. If custom commands or user-shell entry are unavailable, record that instead of substituting prose or assistant tool activity.
- Request one bounded read-only subagent if supported; collect its actual related records and distinguish task summary from child conversation.
- Select a second available model through the observed menu, obtain a context-dependent response, and verify native model identity. Exit and resume the exact recorded main session through the normal interactive CLI.

## Persistence and attachments

Identify this capture's session ID, associated event logs, database records, sidecars, and attachment dependencies from observed metadata. Preserve native relationships; an event log and database rows can provide different parts of the same session.

Use a consistent supported database snapshot/export when necessary, selecting only this session and its dependencies. Do not copy unrelated session history, modify the live store, or assume copying a live database file alone captures pending WAL data. If selected-session collection is unavailable, record the collection limitation before including private data in any repository bundle.

Use the existing Copilot reader's documented public selection interface for validation. If it cannot select the exact captured log directly, use its supported database/session selection and record that distinction. Do not silently validate a newly discovered or newest unrelated session.

Try image attachment only through an actual terminal attachment/paste control. Verify persisted attachment content/media type and message ownership. Database attachments and event-log references may need separate collection; unavailable references remain explicit.

This reference is a discovery and evidence checklist. It does not assert that all common targets or persistence forms exist in every Copilot CLI version.

## Verified with 1.0.104-3 (2026-10-30)

Re-discover these controls on upgrades. The QUAL-2716 capture used the normal
interactive CLI under tmux with `--no-auto-update --disable-builtin-mcps
--sync-session=off`. Select **Yes** in folder trust, without remembering the
folder globally. Set a finite AI-credit limit and record any adjustment through
`/limits`; the limit is soft and checked after calls. The successful capture used
141.29 displayed AI credits, with a 500-credit ceiling and about 14 minutes of
interactive operation. An initial sandboxed launch failed to initialize and
produced no conversation; do not substitute its provisional ID for the main ID.

### Questions and plans

`ask_user` takes `message` and `requestedSchema.properties`. A string `enum`
produces a single choice; an array with `items.enum` produces multi-select.
Space toggles, Tab switches question fields, Enter submits, and Escape cancels.
Moving onto **Other** opens its text editor immediately; typing and Enter submits
it. Send navigation keys separately and verify each screen: rapidly repeated
Down keys did not reliably move to the intended option. The capture includes the
resulting offered selection and the successful custom-answer retry separately.
Free-text fields and Other preserve commas, quotes, and Unicode. An array answer
can contain offered choices and an Other note together. There are no independent
preview or annotation fields in the observed schema; do not invent them.

The authoritative answer is `human_response.recorded.data.response.content`, a
map keyed by the requested property name. Join it through `data.toolCallId`.
`tool.execution_complete` contains a rendered response that can be ambiguous when
an answer contains commas; do not parse that rendering to reconstruct the map.
Cancelled questions may have a completion without a human-response record.

`/plan <request>` enters Plan mode. `exit_plan_mode` opens its review menu.
Navigate to **Suggest changes**, type feedback directly in that row, and Enter
submits; Enter on an empty suggestion does nothing. Approve with the first menu
choice to preserve manual tool approval. Ctrl+E expands the plan, not its editor.
Ctrl+Y opens the saved plan in the configured editor from the ordinary input UI;
it did not open the editor while the review menu was active. The verified edited
workflow cancelled that review, used Ctrl+Y and Vim to edit/save the plan, then
asked Copilot to reread it and request approval again. Preserve proposed and
edited files and the terminal sequence. The native response has `planContent`,
`approved`, optional `feedback` and `selectedAction`; it has no distinct edited
approval flag. Do not infer one from the marker or menu choice.

### Permissions, tools, skills, and commands

Permission menus accept feedback directly on the denial row. Join
`permission.requested` and `permission.completed` through `requestId`, and join
the decision to its tool through `toolCallId`. Keep cancellation, approval for a
location, ordinary approval, and interactive denial distinct. A narrow
`--deny-tool 'shell(touch)'` on exact-session resume produced a configured refusal
without a permission menu. Its completion had `success: false` and an `error`
object, with no `result`. The rule was limited to that launch and expired on exit.

`bash` can report `success: true` with `shellExecution.exitCode: 7`; wrapper success
is not command success. Retain both facts, the complete error/result objects, and
structured image blocks. Other observed tools were `view`, `edit`, `create`,
`glob`, `grep`, `sql`, `task`, `task_complete`, and `read_bash`. Native notebook
editing was unavailable. Large writes needed retries; failed calls must remain.

Workspace skills live at `.github/skills/<name>/SKILL.md`. Agent invocation uses
`skill`; a repeated invocation can emit `skill.invoked_ref` rather than repeating
the definition. Preserve both tool calls and deduplicate only the session skill
list. `/skills` → `r` refreshes newly added local definitions. Direct
`/<skill-name> <arguments>` is the observed custom slash mechanism: the native
human message retains the invocation, while `transformedContent` contains the
expanded skill and `ARGUMENTS:`. It is not a separate command-definition format.
Avoid publishing global skill-list or session-picker snapshots: those UIs expose
unrelated local configuration/history.

`!<command>` runs a user shell command. It records `tool.user_requested` with
`toolName: local_shell`, and an ID-matched completion marked `isUserRequested`.
The observed result combined stdout and stderr; separate stream ordering is not
persisted. It must not become an assistant-owned tool call or an orphan result.

A short shell can return a background shell ID. Follow-up input was delivered
while that shell ran. Escape twice interrupted foreground handling but did not
kill the background process; it later completed successfully. Pending permission
Escape did record cancellation. Record these actual outcomes, not an assumed
process kill.

### Attachments, children, resume, and compaction

`@red.png @blue.png` in one input attached two images. A later input attached
JPEG, GIF, and WebP. This version retained each original media type and bytes in
`session.binary_asset`; each message attachment's `assetId` links to those bytes.
Display names include an `@` prefix, while native paths name the actual files.
A native `view` of an image is a separate tool result with structured content.

One read-only `task`/explore subagent ran. Its records were inline in the main
`events.jsonl`, identified by top-level `agentId`, with `subagent.started` and
`subagent.completed` linked to the parent task's `toolCallId`. No separate child
transcript was written. Summarize the tagged child messages separately; a task
summary alone is not the child conversation.

`/model` opens a searchable menu; selection changes this session only. Obtain a
short recall response and check `assistant.message.data.model`. `/exit` exits
cleanly; `copilot --resume <exact-session-uuid>` resumes it, with a new trust
prompt and a `session.resume` record in the same file. `/compact` appended a
checkpoint and compaction records without removing the native history in this
capture. Preserve pre/post source snapshots and verify recall without repeating
the answer in the prompt.

### Collection and validation

Collect the selected `~/.copilot/session-state/<uuid>/` after clean exit, including
`events.jsonl`, `workspace.yaml`, `plan.md`, checkpoints, the session-local todo
SQLite database, and relevant rewind files. Exclude transient lock files.
Read selected rows from `~/.copilot/session-store.db` in one SQLite read transaction
for `sessions`, `turns`, `checkpoints`, `session_files`, `session_refs`,
`forge_trajectory_events`, and `assistant_usage_events`. Keep original DDL and IDs;
rebuild a selected-session database from sanitized rows rather than copying the
live store or unrelated search indexes. Snapshot the session-local database with
SQLite backup, not a bare copy that can miss WAL data.

Use the shared `sanitize_capture` API for JSONL, exported rows, text sidecars,
terminal evidence, and asset definitions. A narrowly scoped text pass also covers
path-valued JSON keys. Rebuild SQLite from the sanitized data and check rows,
foreign keys, and repeatable bytes. Binary image bytes are copied unchanged after
visual inspection. Hash raw and sanitized files and retain per-rule counts.

The public CLI selects `--db <selected-store> <uuid>`; it has no direct JSONL or
state-root flag. For ordinary offline CLI checks use an isolated HOME containing
the selected state layout. In-process tests can set `copilot_sources.COPILOT_STATE_ROOT`.
Use `read_source`/`interpret_source` and the shared exporter for uncapped model
validation. `copilot_interactions.subagent_summaries` separately summarizes inline
children. Structured questions, decisions, identities, shell-result records,
schemas, and child summaries remain model semantics omitted by the current JSON
schema; complete tool inputs/results/errors, images, and shell-rendered text are
exported. Record that boundary explicitly.
