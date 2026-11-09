# Extraction preservation and fixture guide

All three extractors produce JSON through a reader, a harness interpreter, and
the shared exporter. This guide explains what must survive and how to assess
native evidence. Exact types and optionality belong in the
[shared model](../../src/session_log_extractors/session_model.py); exact JSON
mapping belongs in the [exporter](../../src/session_log_extractors/session_log_envelope.py).
See the [architecture](../../docs/architecture.md) for responsibility boundaries.

## Preservation principles

Preserve existing conversation content and order, session context, complete tool
inputs, nested native payloads, images, and recorded ownership. Recorded Markdown
is content; generated Markdown presentation, duplicate display summaries, and
external skill/command definition loading have been retired.

Interpretation establishes meaning from native evidence before export. Recorded
IDs, native containment, and explicit relationships can establish ownership;
matching counts, names, text, timestamps, or the most recent human message cannot.
Repeated calls and repeated text remain distinct occurrences. Preserve unmatched
records explicitly rather than inventing an association.

Structured facts attach to the conversation record they describe. Internal
session collections index those facts or retain unassociated evidence; they do
not automatically become top-level JSON. Removing internal indexes must not
change export of already attached facts. Export preserves established ownership
and applies configured output policy without changing the interpreted session.

Unknown tools retain their complete opaque payloads and are reported as
unsupported. Recognition is harness-specific: identical tool names need not
mean identical schemas. Recognition does not validate every payload at runtime,
and shared types do not enforce semantic reference integrity at runtime.

## Shared text authorship contract

Preserve unchanged text, including wrappers, whitespace, and literal harness-like
markup typed by a human. Authorship ranges describe evidence over that text;
they use Unicode code-point offsets, not UTF-8 byte positions. Rendered role
alone does not establish an author. Missing ranges, uncovered text, and explicit
unknown ranges all mean unknown attribution.

Candidate activity includes supported native notices of deliberate human
control actions, such as interruptions and ID-linked shell outcomes; their
wording need not have been literally typed. Automatically supplied context and
primary-assistant replies remain harness activity. Another agent's contribution
requires separately recorded identity. Partial evidence must not turn the
remaining message into candidate text. Text attribution does not reassign tool
or image ownership.

Acceptance examples:

- A typed message containing XML-like tags retains those exact words and its
  evidenced candidate authorship.
- A message containing both submitted input and injected instructions retains
  both, with separate evidenced ranges; no synthetic human message is needed.
- An unsupported or contradictory origin remains unknown, with original text and
  retained provenance available for inspection.

The harness provenance guides below distinguish captured classifications from
controlled boundaries, including native IDE attribution that remains unestablished.

This controlled JSON example illustrates text coverage, not a native capture or
a complete envelope. IDE attribution here is illustrative; it does not establish
native IDE evidence. The emoji counts as one code point.

```json
[
  {"i": 0, "role": "user", "text": "Hi 🙂", "authorship": [
    {"start": 0, "end": 4, "source": "candidate"}
  ]},
  {"i": 1, "role": "user", "ts": "2026-10-14T20:27:53.744Z", "text": "Hi 🙂<notice>context</notice>", "authorship": [
    {"start": 0, "end": 4, "source": "candidate"},
    {"start": 4, "end": 28, "source": "harness", "markers": [{"field": "block.type", "value": "notice"}], "ts": "2026-10-14T20:27:53.743Z"}
  ]},
  {"i": 2, "role": "user", "ts": "2026-10-14T20:27:53.744Z", "text": "IDE context", "authorship": [
    {"start": 0, "end": 11, "source": "ide", "markers": [{"field": "source", "value": "editor"}]}
  ]},
  {"i": 3, "role": "user", "text": "Unattributed", "authorship": [
    {"start": 0, "end": 12, "source": "unknown"}
  ]},
  {"i": 4, "role": "user", "text": "Missing evidence"},
  {"i": 5, "role": "user", "text": "Known unknown", "authorship": [
    {"start": 0, "end": 5, "source": "candidate"}
  ]}
]
```

## Shared questions and responses contract

Questions and responses are separate records, attached as `interaction_questions`
and `question_responses` on evidenced calls under `events[].tool_calls[]`.
Native question keys scope answers within their owning call. Local references
address occurrences after interpretation; they do not supply missing evidence.
Recommended options are not selections. Preserve offered labels/descriptions,
previews, selected options, free text, and candidate notes separately where the
native format records them.

Acceptance examples:

- A multi-select answer names unique offered options without selecting an option
  merely because its label occurs inside a note or another label.
- Notes-only answers retain notes without inventing a selection. A selected
  preview and candidate feedback can coexist with an actual selection.
- Repeated identical questions, duplicate IDs, or unmatched answer keys do not
  acquire guessed associations. A question without a response stays unanswered.
- An unrelated tool error establishes neither refusal nor cancellation.

Claude and Copilot can retain unowned responses on result-only conversation
activity. Codex unmatched results without a conversation owner remain internal.
Containment on a result record does not establish which individual question was
answered: an unresolved question association stays null.

This controlled JSON excerpt illustrates exported attachment placement and
distinct answer states; it is not a native capture or a complete envelope.
It omits export-generated byte accounting for readability.

```json
{
  "harness": "illustrative",
  "session_id": "questions-example",
  "events": [
    {
      "i": 0,
      "role": "assistant",
      "tool_calls": [
        {
          "name": "question-tool",
          "interaction_questions": [
            {
              "ref": "q1",
              "native_id": "request-a",
              "text": "Choose?",
              "multiple_selections": true,
              "options": [
                {
                  "ref": "o1",
                  "label": "A (Recommended)",
                  "description": "First option",
                  "recommendation_markers": [
                    {
                      "field": "native.options[0].label",
                      "value": "A (Recommended)"
                    }
                  ]
                },
                {
                  "ref": "o2",
                  "label": "B"
                },
                {
                  "ref": "o3",
                  "label": "C"
                }
              ]
            },
            {
              "ref": "q2",
              "native_id": "request-b",
              "text": "Choose?",
              "options": []
            },
            {
              "ref": "q3",
              "text": "Anything else?",
              "options": []
            }
          ],
          "question_responses": [
            {
              "ref": "r1",
              "outcome": "answered",
              "question": {
                "question_ref": "q1",
                "evidence": [
                  {
                    "field": "native.question.id",
                    "value": "request-a"
                  },
                  {
                    "field": "native.answer.question_id",
                    "value": "request-a"
                  }
                ]
              },
              "selections": [
                {
                  "option_ref": "o2",
                  "label": "B"
                },
                {
                  "option_ref": "o3",
                  "label": "C"
                }
              ],
              "free_text": "Also keep this text"
            },
            {
              "ref": "r2",
              "outcome": "refused",
              "question": {
                "question_ref": "q2",
                "evidence": [
                  {
                    "field": "native.question.id",
                    "value": "request-b"
                  },
                  {
                    "field": "native.refusal.question_id",
                    "value": "request-b"
                  }
                ]
              },
              "refusal": {
                "by": "candidate",
                "explanation": "Prompt dismissed"
              },
              "candidate_feedback": "Please ask later"
            }
          ]
        },
        {
          "name": "unrelated-tool",
          "result": "Tool failed",
          "is_error": true
        }
      ]
    },
    {
      "i": 1,
      "role": "user",
      "tool_results": [
        {
          "tool_call_id": null,
          "ts": null,
          "is_error": false,
          "content": "Recorded response without a call owner",
          "question_responses": [
            {
              "ref": "r3",
              "outcome": "answered",
              "question": null,
              "selections": [],
              "free_text": "Unassociated answer"
            },
            {
              "ref": "r4",
              "outcome": "unknown",
              "question": null
            }
          ]
        }
      ]
    }
  ]
}
```

Inspect the harness question tests for exact native parsing boundaries:
[Claude](../test_claude_questions.py), [Codex](../test_codex_questions.py), and
[Copilot](../test_copilot_questions.py).

## Shared permission refusals and plan decisions contract

`permission_refusals`, `plan_versions`, `plan_presentations`, and `plan_decisions`
attach to evidenced messages, calls, or results. Keep the refused action, native
variant, source, complete plan text, candidate feedback, and harness explanation
where recorded. Native permission request/completion lifecycles are separate
from an established refusal.

Acceptance examples:

- A settings refusal remains settings activity, not a human rejection. A tool
  error, cancellation, or assistant refusal-like prose alone is not a refusal.
- A plan proposal does not prove presentation for approval. Missing decisions
  do not imply approval; unfamiliar outcomes stay unknown.
- An edited plan can be approved, but changed text alone does not establish an
  edit relationship or identify the previously displayed version.
- Later human feedback stays separate unless recorded evidence establishes its
  relationship to a decision. Complete feedback must not be replaced by a
  harness-generated explanation.

See the [shared decision tests](../test_session_decisions.py) and production
[Claude](../test_claude_decisions.py), [Codex](../test_codex_decisions.py), and
[Copilot](../test_copilot_decisions.py) tests. These exercise current attachment
behavior; the removed legacy question/decision summaries are not another source
of interpretation.

## Shared file actions, subagent tasks, and invocations contract

All three adapters now supply `file_actions`, `subagent_tasks`, and `invocations`
on evidenced messages or calls. Facts supplement complete inputs/results rather
than replacing them with display summaries. Full prompts and descriptions are
independent: a short description must not replace a recorded prompt.

Acceptance examples:

- A successful edit without per-path modification evidence has no confirmed
  change. A request, success flag, generated assertion, or legacy `files_changed`
  entry alone does not prove modification. Absence of confirmation does not
  prove the file was unchanged. Confirmation of one path does not confirm every
  path in a batch.
- Opaque shell/code text stays intact without being executed or expanded into
  invented nested tools, file paths, or changes.
- A task retains its full recorded prompt, even if opaque, and its independently
  recorded description. Child identity requires an evidenced relationship.
- An invocation and its explicitly linked definition enrich one occurrence;
  another actual invocation remains separate. Standalone links, names, paths,
  or definitions do not establish an invocation.
- Exact slash text, arguments, links, and definitions already present in source
  remain available. Definitions are never loaded from external paths.
- Unlinked database file activity remains internal with unknown conversation
  ownership. Export must not attach it to a nearby tool call.

See the [shared action tests](../test_session_actions.py) and production
[Claude](../test_claude_actions.py), [Codex](../test_codex_actions.py), and
[Copilot](../test_copilot_actions.py) tests.

## Export acceptance

Export the same interpreted session at textual result budgets **0, 17, 4096,
and -1** (unlimited). Structured questions, answers, decisions, file actions,
task text, and invocation information attached to exported records remain
complete even at zero. Native structured objects/lists and their nested strings
also remain complete. Each textual result representation has independent byte
accounting and truncation provenance; missing, null, and empty remain distinct.

Check valid UTF-8 at an actual multibyte split, original byte counts, repeated
export without mutation, and file/stdout parity. Message text, tool inputs, images,
structured facts, and nested payloads are not shortened by the textual cap.
The default is **8,192 bytes**. The **8 MiB** warning measures the complete
serialized envelope, including images; it does not drop more content.

The [export-policy tests](../test_export_policy.py) and
[native-fixture stage contract tests](../test_extractor_contract.py) cover these
boundaries. JSON remains `raw-session-log/1` for additive enrichment; incompatible
changes of existing fields require a new version. General runtime semantic
validation is not implemented.

## Native evidence and limits

Each selected complete capture retains native structure and required dependencies
with sanitization. Provenance distinguishes native observations from controlled
mutations and focused legacy cases:

| Harness | Selected capture and evidence limits |
| --- | --- |
| [Claude](claude/README.md) | Interactive 2.1.312 with 2.1.314 resumes; sidecar child, native questions/previews/notes, refusals, and edited-plan evidence. Peer-origin cases are controlled boundaries; native IDE attribution remains unestablished. |
| [Codex](codex/README.md) | Interactive 0.166.0 main and child rollout; question answer arrays, skills, file completions, settings refusal, and agent deliveries. Multi-select/previews, a notes-only answer, edited-plan editor, and successful custom-command arguments were not established. |
| [Copilot](copilot/README.md) | Interactive 1.0.104-3 with selected SQLite rows and inline child records; native answer maps, permission sources, and returned plan text. No native notebook editing or question preview/annotation fields. Changed editor assets do not establish a native edit relationship. |

Claude sidecar summaries are separate from main JSON. Codex children can be read,
interpreted, and exported separately using their history boundary. Copilot children
are inline with agent scope; their separate summaries remain internal. A parent's
task summary is not evidence of the complete child's conversation.

Capture provenance reports historical operation and sanitization; private journals,
raw originals, and private rules are not ordinary test inputs. Do not claim to
have revalidated that private history merely by passing committed-fixture tests.
No capture proves every native variant or unsupported workflow.

## Regeneration and fixture acceptance

Use the [capture skill](../../.agents/skills/capture-native-session/SKILL.md)
for explicitly authorized paid capture, outside tests and CI. The scripted
headless generators and fake-CLI tests have been retired. Ordinary tests use
committed evidence and controlled boundaries. Approved whole-output baselines
and an automated fresh-capture drift workflow are unfinished.

Select native evidence by tool names, structured properties, prepared assets,
and operator markers, then associate selected records through recorded IDs.
Do not pin incidental generated wording, transcript positions, saved IDs, model
names, or generated shell syntax. Regeneration must tolerate reworded questions,
labels, corresponding answers, and assistant prose without approving a new prose
baseline. Missing required feature evidence must fail explicitly.

For variable prose, assert nonempty strings where required; for selections, assert
counts, uniqueness, and membership in recorded options. Compare preserved payloads
directly to recorded fields/assets. Keep exact expectations for deliberately
controlled parser cases, ownership, meaningful order, images, decision states,
and byte limits. Do not build a second extractor to derive an oracle through
the same production logic. See [AGENTS.md](../../AGENTS.md) for the authoritative
test policy and all five required 95% coverage gates.

Consumer presentation and historical Markdown-upload handling belong to the
consuming application. This guide does not establish a general planning lifecycle,
child graph, or new display model as an extractor requirement.
