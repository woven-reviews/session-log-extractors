---
description: Investigate any disagreement between the two Code-of-Conduct detectors on the Andela assessment platform — assessment-science (Qualified authenticity) vs Woven's robot scorers. Find a contested solution, pull its raw edit timeline from Qualified production, reconstruct what the candidate actually did, identify *why* the two systems disagree (capability gap, threshold difference, policy gating, mitigation over-credit, session-averaging, a bypass, challenge-type miscalibration, …), and recommend or implement a fix. Use whenever someone wants to compare AS vs Woven CoC verdicts, dig into why the two systems disagree on cheating / plagiarism / AI use / pasting / transcribing / second-device typing, find what authenticity detection is missing, audit a suspicious-behavior false negative or false positive, or harden the platform's cheating detection.
allowed-tools: Read, Glob, Grep, Bash, Edit, Write, Agent, AskUserQuestion
---

You are investigating a disagreement between two independent Code-of-Conduct (CoC)
detectors on the Andela assessment platform:

- **assessment-science (AS)** — the authenticity engine behind Qualified. Produces
  a `rating` (1=inauthentic … 5=authentic), `confidence`, `primary_concern`, and a
  policy-gated `coc_violated` boolean.
- **Woven robot scorers** (apply-yourself) — independent scorers that set a
  candidate's `code_of_conduct_status`.

They can disagree for **many different reasons**, about **many different
behaviours** — large pastes, transcribing from a second screen, AI assistance,
external-IDE development, abnormal pace/time investment, attention/blur patterns,
or pure policy/definitional differences. **Do not assume the divergence is about
pasting** (that is just one worked example below). Let the data tell you what kind
of disagreement this is, then find the precise cause.

The goal is to turn a single disagreement into either a concrete, defensible
improvement to one of the systems, or a clear finding about which system is
miscalibrated — without assuming either system is correct.

**Read `scripts/coc-investigation/REFERENCE.md` now.** It has the code map, data
schema, exact script invocations, the divergence-type map, the cause taxonomy, and
the methodology principles. The three principles are load-bearing and easy to get
wrong:

1. **No ground truth** — neither system is assumed right; justify every directional claim.
2. **`coc_violated` is policy-gated** — AS's real signal is `rating`; compare on `rating <= 3`.
3. **Scope out `coc_ai_usage_allowed = true`** — genuine gaps live on AI-disallowed challenges.

All scripts referenced below live in `scripts/coc-investigation/`. Per repo
convention, prefix every `Bash` call with an absolute `cd` so cross-repo commands
don't run against the wrong child repo.

## Phase 0 — Orient and refresh the data

Check `data/qualified_coc_data.json` and `data/woven_coc_data.json` exist and look
current. If missing or stale, re-pull with the export scripts (see REFERENCE.md for
exact commands). They run **standalone** (`bundle exec ruby`, not `rails runner` —
no production boot, no `cache_classes` hack), are **read-only**, and write straight
into `data/`. Qualified reads creds from `repos/qualified/rails/api/.env.production`;
Woven takes any Postgres source via `WOVEN_DATABASE_URL`. State the dataset sizes
and match count before going further.

## Phase 1 — Find a contested case (across any divergence type)

Run the neutral comparison and read its output end to end:

```
cd /Users/<you>/andela-assessment-planning && ruby scripts/coc-investigation/neutral_analysis.rb   # reads data/ directly
```

It reports agreement at two levels (policy-gated `coc_violated` vs raw `rating`)
and splits disagreements into **Set A** (AS flags, Woven silent) and **Set B**
(Woven flags, AS clears).

**Characterise the divergence space before picking.** The two systems describe
different behaviour families; map them (full table in REFERENCE.md):

| Woven `code_of_conduct_status` | Likely AS `primary_concern` analogue | Behaviour family |
|---|---|---|
| `*_large_paste` | Same-Device AI + Paste / External Drafting / External IDE | bulk paste |
| `*_transcribing` | Second Device AI + Manual Typing | transcribing / linear typing from a second source |
| `normal` (but AS flags) | any | AS-only signal Woven has no scorer for |

Pick the **direction** by intent — to find what **AS is missing**, use Set B; to
find what **Woven is missing**, use Set A (e.g. Second-Device cadence, which Woven
has no scorer for). Then choose the **starkest single case in the family you're
investigating**: high confidence on both sides, most opposed, `coc_ai_usage_allowed =
false`. Write a one-off Python/jq filter over the data files (filter by the Woven
status family *and/or* AS concern you care about — not just paste) and pick the
highest-confidence candidate. Note its `solution_id` and `challenge_id`.

If the direction or family is genuinely ambiguous and the choice changes the
conclusion, ask with `AskUserQuestion`; otherwise pick the obvious starkest case
and say which and why.

## Phase 2 — Pull the raw timeline from Qualified production

The summary export only has rating/confidence/concern. To see what AS actually saw,
pull the solution's full revision timeline (read-only):

```
cd /Users/<you>/andela-assessment-planning/repos/qualified/rails/api
bundle exec ruby \
  /Users/<you>/andela-assessment-planning/scripts/coc-investigation/deepdive_solution.rb <SOLUTION_ID>
```

(Standalone — no `rails runner`, no production boot, no `cache_classes` hack.)
This writes `/tmp/deepdive_solution.json` and prints the challenge title, duration,
tag counts, AS verdict + feedback, and the tagged event timeline. **The challenge
title and type matter** — a "Message"/written task is not a coding task, and
code-tuned heuristics misfire on prose; a long time-limit vs a short one changes
what "fast" means.

## Phase 3 — Reconstruct what the candidate actually did

```
cd /Users/<you>/andela-assessment-planning && python scripts/coc-investigation/summarize_timeline.py
```

This prints a multi-lens overview of the session. **Read it, decide which lens the
divergence points to, then dig into that lens** using the raw timeline + content
previews in `/tmp/deepdive_solution.json`. The lenses (and what each tells you):

- **Pasting** — paste events, origin (external vs same/other-file), and the
  velocity test: could the candidate have typed this much in the gap since their
  last edit, at their own pace? A volume far beyond their rate that appeared in one
  revision was composed outside the editor.
- **Transcribing / second-device typing** — steady typing cadence with no thinking
  pauses, long runs of add-only (linear) edits, low rework, and **final-convergence**
  (early edits already match the final answer = copying a known solution). Woven
  calls this "transcribing"; AS calls it "Second Device AI + Manual Typing".
- **Time & pace** — total duration vs active time, completion far faster than peers,
  an implausibly large first meaningful edit.
- **Attention** — focus/blur events clustered around big additions (reading from a
  second device).

Also read the actual content: look for **human typos / personal voice** (a person
drafted it, possibly elsewhere) vs **polished, uniform prose or idiomatic code**
(possible AI), and whether early content already equals the final answer. Note the
challenge's policy (`coc_ai_usage_allowed`, `coc_external_ide_allowed`) — the same
behaviour is a violation on one challenge and allowed on another.

Write down, in plain terms, the most defensible reading of the timeline **and what
would make the opposite reading true.**

## Phase 4 — Identify the cause of the divergence

Explain *why* the two systems landed differently. Read AS's feedback (in the dump),
then trace the relevant code. Use the **cause taxonomy in REFERENCE.md** — it is
not paste-specific. The usual causes:

- **Capability gap** — one system structurally cannot see the signal (e.g. Woven
  has no scorer for cross-device cadence/convergence; AS may lack a signal Woven
  has). The other system's silence is then *uninformative*, not a clear.
- **Threshold difference** — both look at the same signal, one fires, one doesn't.
- **Policy / definitional** — `coc_violated?` suppressed a flag AS raised (config),
  or the systems are answering different questions (challenge-aware vs agnostic).
- **Mitigation over-credit** — AS's signed-points anti-patterns (edit density,
  rework, engagement) cancelled a real signal.
- **Session-averaging dilution** — a check divides a total by whole-session time, so
  a localized event is washed out; needs an event-local check.
- **Bypass** — a check returns "no concern" under a guard condition that doesn't hold.
- **Challenge-type miscalibration** — code-tuned heuristics applied to a prose task.

For broad code tracing across `assessment-science/.../checks/`, `scoring/`,
`profiles/`, and the Woven scorers in `apply-yourself/app/services/robot_scorer/`,
spawn an `Explore` agent rather than reading everything yourself. Pin the exact
mechanism with `file:line`. Characterise it **neutrally** — AS and Woven are often
answering different questions, so "different" is not automatically "wrong".

## Phase 5 — Recommend or implement the fix

First decide **which system has the gap and what kind** — that determines the fix:
- If **AS is missing a real signal** → add or adjust an AS check (most common).
- If the cause is a **capability gap on Woven's side** → the recommendation is a new
  Woven scorer; note both sides and which repo owns it.
- If it's **policy/definitional** → there may be no code fix, just a finding (or a
  config correction).

For an AS check, prefer signals that are **event-local** (judged on their own, not
diluted by a session average), **candidate-relative** (compared to the candidate's
own behaviour, so outliers like fast typists aren't punished), and **not subject to
a bypass** that would re-suppress them. (Worked example from the first run:
`copy-paste/external-paste-exceeds-typing-rate` — a paste too large to have been
typed in the elapsed gap. See REFERENCE.md "Shape of a good fix".)

To implement an AS check:
1. Add the `CheckDefinition` in the appropriate `checks/*.ts` (score 1 = no concern,
   0 = max concern). Compute from existing event fields where possible.
2. Wire it into the relevant `profiles/default-code.ts` pattern(s) with calibrated
   points; bump the profile `version`.
3. Add vitest cases in `checks/tests/`: one that fires on the case shape, plus
   negatives (a benign variant, an outlier the candidate-relative logic should
   protect, and a below-floor/threshold case). Run
   `cd /Users/<you>/andela-assessment-planning/repos/assessment-science/web/apps/analysis-app/nitro && npx vitest --run <file>`.
4. Run the surrounding suite; report any pre-existing failures separately.

When the fix is a thresholded code change, always end by stating its tunable
thresholds and recommending calibration against the broader contested set
before shipping — a single case justifies the *mechanism*, not the exact
numbers. (See `calibrate_export.rb` / `calibrate_sweep.py`.) For a
policy/definitional finding with no code fix, skip this — there are no
thresholds to report, and inventing one is worse than omitting it.

## Output

Produce a short written findings summary: the case (ids, challenge, both verdicts),
the reconstructed timeline, the identified cause with `file:line`, and the
recommendation (or the diff + test results if implemented). Keep the language
neutral — "disagreement" and "gap", not "AS is wrong" — unless you have
ground-truth-free proof (internal contradiction, definitional difference,
capability gap, or the timeline itself).
