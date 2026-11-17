# Copilot event-source audit (QUAL-3020)

Copilot extraction discovers and reads `session-state/*/events.jsonl` without
opening SQLite. `--db` has been removed; `--state-root DIR` selects an alternate
session-state directory. Default discovery remains under `~/.copilot`.

GitHub describes the complete local session record under `session-state/` and
SQLite as a subset used for cross-session queries. See [session data](https://docs.github.com/en/copilot/concepts/security-governance-and-network-settings/session-data)
and the [configuration directory reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference).
These documents establish storage roles, not internal persistence ordering.
The mappings below use the committed CLI 1.0.104-3 capture.

## Consumed database fields

| Previous database input | Previous purpose | Native evidence and decision |
| --- | --- | --- |
| `sessions.id` | Selection, model/envelope identity, filename prefix | `session.start.data.sessionId`; directory names never supply identity. Conflicting or missing recorded IDs cannot be exported. |
| `sessions.cwd` | Directory selection and envelope cwd | Original `session.start.data.context.cwd`; resume context only when start cwd is unavailable. |
| `sessions.created_at` | Start time, filename date, batch order | `session.start.data.startTime`; preserve across resume. Without it, export null start time and omit the filename date. |
| `sessions.updated_at` | Newest selection | Maximum parseable recorded event timestamp, including resume and subsequent activity, instead of delayed database indexing. Unknown activity sorts before known activity. |
| `sessions.repository`, `branch`, `summary` | Selected but never interpreted or exported | No replacement needed. Native lifecycle context remains in the loaded source. |
| `turns.turn_index` | Order fallback prose and associate fallback file summaries | Native record order and recorded message/tool IDs establish conversation and ownership. DB turn numbering is an index, not a native conversation address. |
| `turns.user_message`, `assistant_response`, `timestamp` | Conversation fallback when native messages are absent | Native messages preserve complete content, order, timestamps, scope and authorship. Tool events preserve execution evidence. Database-only recovery is intentionally removed. |
| `session_files.file_path`, `tool_name` | Internal activity and fallback synthetic tools/actions | Native tool inputs and completion `fileEdits` retain paths and names with recorded owners, outcomes and change evidence. All three captured DB path/tool pairs have native equivalents. |
| `session_files.turn_index`, `first_seen_at` | DB provenance, fallback order/timestamps | DB index and observation time are not native IDs or execution times. These indexing facts are intentionally no longer loaded. Native actions retain their own conversation/result evidence. |
| `attachments.display_name`, `path`, `type` | Optional legacy image lookup | Owning `user.message.data.attachments` and `session.binary_asset` supply names, paths, MIME types and embedded bytes. External images are snapshotted by the reader. The capture has no separate DB attachments table. |
| `assistant_usage_events.model` ordered by `id` | Ordered distinct model IDs | Ordered distinct `assistant.message.data.model`, including children. All three captured models match in order. Model configuration and internal utility-model calls do not establish conversation producer use. |
| Legacy `events.usage_model` ordered by `timestamp` | Fallback for assumed older SQLite schema | Removed with database support; recent native producer evidence is authoritative. |

`get_session_checkpoints` and `get_session_refs` were unused database utilities.
Checkpoint number/title/overview/creation time and ref type/value/index/time did
not enter interpretation or envelopes. These SQLite helpers are also removed.
Table/column probes and row IDs were schema/ordering mechanisms.

No required conversation or session fact in this supported capture requires
optional database supplementation. Captured databases and readable JSON exports
remain committed for provenance and audit.

## Baseline comparison

Before editing, read the sole committed native Copilot session,
`07640183-dc62-4ab4-813f-d9f285a63b90`, with the pre-change extractor at `77f9f6a`.
Point its state root at the committed event directory and use its committed DB.
Save sorted JSON envelopes at byte limits **0, 17, 8192, and -1**, plus an
uncapped shared model. Run the event-only reader against the same event file
with equivalent image availability and export settings.

All four envelopes are identical: 105 ordered messages with unchanged identities,
inputs, results, images, questions, decisions, authorship and scope. Embedded
assets are available in both runs; unavailable sanitized external paths remain
equally unavailable. This is a one-time migration comparison, not a generated
prose baseline test. Native preservation tests remain wording tolerant.

The uncapped model has exactly two differences:

- `file_activity`: the three DB summary/provenance rows are no longer populated.
- `file_actions`: 33 becomes 30, removing three unassociated DB summary actions.
  Every native action and attached fact remains unchanged. Each removed summary
  has native path/tool evidence, but cannot identify a particular call when that
  path is used repeatedly.

Database-only sessions, missing logs, blank/identity-free logs and logs whose
identity cannot be established no longer recover indexed prose or synthetic
file calls. This is an intentional removal of the old fallback. Lifecycle-only
logs still export empty envelopes.

## Selection and incomplete evidence

Exact case-sensitive IDs precede unique literal prefixes. Duplicate recorded IDs
are ambiguous even in different directories. Discovery scans all records and
uses only unscoped lifecycle evidence for root identity/cwd. First nonempty start
cwd and start time remain authoritative; resume cwd is a fallback only. A
resume-only file without recorded identity is diagnosed and excluded.

Missing metadata stays unknown. Newest selection uses aware UTC timestamps
(naive timestamps mean UTC), then start-time text, recorded ID and path to break
ties deterministically. Filesystem modification times never establish activity.
Cwd overlap, strict equality, single-session global fallback, default batch
export, stdout and collision suffixes retain their contracts.

Discovery diagnoses unreadable, wholly invalid and identity-free candidates and
continues so unrelated bad sources cannot defeat valid explicit selection.
An explicit ID with no usable candidate fails with its selector and source
warnings; no output is written. Selected-source read/output failures remain
errors. Batch attempts all selected candidates and retains successful exports.
Mixed malformed/non-object lines use the shared bounded JSONL diagnostics.

## Downstream and data impact

Under QUAL-2324, bump the assessment package pin and remove `--db` from
`EnvelopeBuilder.copilot`. For alternate input directories, supply the
corresponding `--state-root`. Change its missing-database expectation to success
when event evidence is usable.

The old bug could omit captured turns while native events still existed; it did
not modify native session data. This change reads sources only and does not
repair historical exports or re-send data to other systems. Historical capture
repair and assessment-repository changes remain separate work.

To locate potentially affected capture attempts in retained hook logs, search:

```sh
rg -n -e "Copilot database not found|no session found matching" <capture-hook-log-directory>
```

These failures identify attempts to investigate, not proof that later captures
also omitted the turn. Correlate their recorded session IDs with existing exports.
This repository has no production capture database or downstream delivery state;
automatic re-delivery would require separately checking downstream idempotency
and side effects.
