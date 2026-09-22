---
description: Write an implementation spec for a Linear ticket and post it as a comment. Always stops before implementing — use /work-ticket to execute the spec. Use with a QUAL-XXXX ticket identifier.
allowed-tools: Read, Glob, Grep, Bash, Agent, AskUserQuestion, mcp__claude_ai_Linear__get_issue, mcp__claude_ai_Linear__list_comments, mcp__claude_ai_Linear__save_comment
context: fork
---

You are writing an implementation spec for a Linear ticket. You will read the ticket, explore the codebase, and produce a short spec posted as a Linear comment. You do not implement anything.

**One spec comment per ticket, ever.** A ticket's spec lives in a single comment. Never post a second comment to record a revision, and never use the ticket's comment thread as a scratchpad for iterating toward a spec — iterate first (with the user, or across your own passes), and only call `save_comment` once you have the version you'd stand behind. If the spec changes later — new information, a correction, a design pivot — edit that same comment in place with `save_comment`'s `id` param. Posting to a shared ticket is visible to the whole team; each new comment is a separate disclosure, not just a save.

## Arguments

`/write-spec QUAL-XXXX` — the Linear ticket identifier.

## Step 1: Read the ticket

Fetch the ticket with `get_issue`, and its comments with `list_comments`. Read the title, description, any comments, and attached links.

**If a comment already starting with `## Spec` exists**, this run is a _revision_, not a first pass: note its comment `id` — Step 4 will update it in place rather than create a new one — and read it as the starting point to revise, not context to ignore.

**Before continuing:** if the ticket is too vague to spec — no clear success criteria, contradictory requirements, or missing context that can't be found in the codebase — stop and report back to the user. Do not invent a spec. Ask the specific question that needs answering first.

## Step 2: Gap analysis — what exists, what needs to change

Use the Agent tool (subagent_type: Explore) to locate the files relevant to this specific ticket. Scope exploration to what the spec actually needs:

- For a frontend-only change, don't map backend services
- For a model/service change, don't explore unrelated UI components
- Stop once you have enough to describe current behavior precisely — don't over-explore

Before drafting anything, post a short gap analysis **to the user in chat** (not to Linear):

- **What exists today** — the current behavior at each relevant touchpoint, with file paths
- **What's missing or needs to change** — the gap between that and what the ticket asks for
- Anything you found that contradicts, duplicates, or complicates what the ticket assumes (stale code, an abandoned parallel implementation, a constraint the ticket didn't mention)

This is a checkpoint, not a formality — give the user a chance to correct your reading of the codebase before any spec ideas get drafted on top of it.

## Step 3: Iterate on the spec with the user

Draft spec ideas and work through them with the user in conversation — this is a back-and-forth, not a one-shot generation. Surface the decisions the ticket leaves open (approach, scope, edge-case handling) as concrete options rather than picking silently; use `AskUserQuestion` for discrete choices and plain conversation for open-ended design discussion. Revise based on what comes back, as many rounds as it takes.

**Do not post anything to Linear during this step.** The comment thread is not where this iteration happens (see the rule above). Keep drafting in chat until the user explicitly approves the spec — a clear "yes," "looks good," "post it," or equivalent. If the user goes quiet on an open question, that is not approval; ask again rather than assuming.

**Length constraint once settled: total spec under 20 lines. Each section 1–4 lines.** If a section wants to be longer, the ticket is too big — note that it should be split and stop.

**What belongs:**

- Exact entry point(s) — specific file paths
- Current behavior — one sentence per touchpoint
- The delta — what changes, not a system description
- Constraints — decisions that were made during iteration and why, so `/work-ticket` doesn't have to guess or re-litigate them
- Acceptance criteria — 2–3 observable outcomes

**What does NOT belong:**

- How to implement it (Claude's job in `/work-ticket`)
- Background, history, or context Claude can infer from reading the files
- A transcript of the iteration itself — the spec is the settled result, not the discussion that produced it

Format:

```
## Spec

### Entry point
<Exact file paths where the change starts — one path per line>

### Today
<Current behavior, one sentence per relevant touchpoint>

### Change
<The delta — what's different after this ships>

### Constraints
<Decisions settled during iteration that Claude would otherwise guess wrong: library, pattern, edge case handling, what to leave alone>

### Done looks like
<2–3 acceptance criteria — observable outcomes, not implementation steps>

### Open questions
<Only include if the ticket CANNOT proceed without an answer that iteration didn't resolve. Style preferences and nice-to-haves are not open questions — make a decision and put it in Constraints. If none, omit this section entirely.>
```

## Step 4: Post to Linear

Only after the user has explicitly approved the spec text. Post it as a comment using `save_comment` — pass the existing spec comment's `id` from Step 1 if this is a revision, so it updates in place instead of creating a new comment. Replace all angle-bracket placeholders with actual content — do not include the brackets in the final comment.

## Step 5: Report back

This skill has no other side effects beyond the Linear comment — it doesn't touch git at all. `/work-ticket` creates the branch and its worktree itself, exactly when implementation starts; a branch reserved here with nothing on it yet would just be dead state waiting on a spec that might still be revised, or never picked up.

Do not move the ticket status — `/work-ticket` will transition it to "In Progress" when real work begins.

Report back to the user with:

- A link to the ticket
- Any open questions that must be answered before `/work-ticket` can run
- If none: confirm the spec is ready to execute
