# JSON extraction architecture

This is the target architecture for extracting native agent sessions into JSON.
The [extraction information contract](../scripts/fixtures/README.md) defines what
must survive. Frontend presentation belongs to the consuming application.

Use one deterministic pipeline: identical recorded inputs and extraction options
produce identical JSON.

```mermaid
flowchart LR
    A[Native session sources] --> B[Harness adapter]
    B --> C[Shared session model]
    C --> D[Shared JSON exporter]
    D --> E[Versioned JSON]
```

| Component | Responsibility |
| --- | --- |
| CLI / orchestration | Select sessions, pass options, choose output destinations, and report errors. |
| Harness adapter | Locate and read native files, database records, sidecars, and assets; interpret their formats, authorship, ordering, and associations. |
| Shared session model | Represent recorded content, structured meaning, and their relationships consistently across harnesses. |
| Shared JSON exporter | Apply result limits, validate the output, serialize JSON, and report its size. |

These are boundaries within one Python package. Each harness owns its native
interpretation. Keep source I/O separable from interpretation so tests can supply
recorded inputs. Adapters may use different reading and joining strategies; they
agree on the session model they produce.

The session model preserves every existing JSON field and its contents, including
nested tool payloads and metadata that consumers do not display. It adds explicit
questions, answers, refusal sources, candidate feedback, plan versions and
decisions, file actions, subagent prompts, and skill/command invocations wherever
the contract requires them. Structured facts remain connected to the recorded
activity supporting them.

Keep the internal model close to the JSON contract. Export mapping exists only
where compatibility or export policy requires it.

The model and its tests enforce these rules:

- Preserve recorded event order, timestamps, repeated calls, and image ownership.
- Distinguish message role from authorship, including mixed candidate and injected
  content.
- Associate answers with questions and results with calls using recorded evidence.
  Counts or timestamp proximity alone do not establish a question–answer link.
- Preserve unassociated answers and unknown sources or decisions without guessing.
- Keep recommendations distinct from selections, tool errors distinct from
  candidate decisions, and attempted edits distinct from confirmed changes.
- Preserve relevant plan versions and distinguish missing results from empty ones.

Lossy processing happens at export. Adapters extract required questions, feedback,
and plan versions from complete results before truncation. The exporter centrally
applies the **8,192-byte default raw-result limit**, configured overrides, and
unlimited mode. It preserves original byte counts, truncation indicators, and valid
UTF-8. It also warns when the complete serialized JSON exceeds **8 MiB**, including
images; the result cap is not a whole-file size guarantee.

Validate both JSON structure and semantic invariants, such as the validity of
asserted relationships. Unreadable or unsupported input produces an actionable
error. Tolerated malformed records produce explicit diagnostics. Missing image
assets retain unavailable markers. Valid sessions with no displayable events
still produce valid empty-session JSON. Diagnostics stay separate from JSON output.

Verification follows these boundaries: adapter tests check interpretation, shared
tests check export policy, and complete-output comparisons check preservation
through the production extraction path. Fixture capture is separate tooling;
ordinary CI uses fixed captures and human-approved baselines without paid CLI
calls or automatic approval. A separate fresh-capture workflow checks upstream
drift. Synthetic tests supplement native evidence.

Exact field names and detailed file layout are settled during implementation.
The [implementation plan](implementation-plan.md) records migration order and scope.
