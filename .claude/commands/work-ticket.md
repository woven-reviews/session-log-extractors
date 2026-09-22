---
description: Implement a Linear ticket from an approved spec using TDD. Runs autonomously — write the spec first with /write-spec, review it, then run this. Use with a QUAL-XXXX ticket identifier.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Task, ToolSearch, EnterWorktree, ExitWorktree, Skill, mcp__claude_ai_Linear__get_issue, mcp__claude_ai_Linear__save_comment, mcp__claude_ai_Linear__list_comments
context: fork
---

You are implementing a Linear ticket autonomously using TDD. A spec has already been written, reviewed, and approved — your job is to execute it. Do not stop to ask for input. Make decisions, note them in the PR description, and keep moving.

## Arguments

`/work-ticket QUAL-XXXX` — the Linear ticket identifier.

## Step 1: Read the ticket and spec

Fetch the ticket with `get_issue`. Fetch all comments with `list_comments`. Find comments starting with `## Spec`.

- If no spec comment exists, stop and tell the user:
  > "No spec found on QUAL-XXXX. Run `/write-spec QUAL-XXXX` first, review it, then re-run `/work-ticket`."
- If multiple spec comments exist, use the most recent.
- If the most recent spec comment was posted before the most recent ticket description edit, warn the user that the ticket may have changed since the spec was written and ask whether to proceed.
- Before trusting the spec, spend one quick pass confirming its **Entry point** files and key factual claims still hold against current HEAD — `grep`/`ls` for named files/methods, or a fast `Explore` pass if a claim is more than file existence. This isn't a full gap-analysis re-run, just a cheap guard against a spec that's gone stale or was wrong from the start. If something material doesn't hold, warn the user and ask whether to proceed.

These two checks are the only input requests in the whole skill.

The spec is your contract. Read **Entry point**, **Today**, **Change**, **Constraints**, and **Done looks like** carefully. You execute against these — how you get there is your call.

## Step 2: Set up an isolated worktree

`/write-spec` doesn't create a branch — this is where the branch and its workspace come into existence together, exactly when they're needed, so this run doesn't collide with the main working directory (another session, the user's own terminal, a parallel `/work-ticket` run).

```bash
git fetch origin
```

Determine this repo's PR base branch first — check its own CLAUDE.md, or the monorepo's `scripts/repo` (`repo_default_branch`) if you're inside `repos/<name>/` — then:

- If `QUAL-XXXX-slug` doesn't exist yet (the normal case): `git worktree add -b QUAL-XXXX-slug .claude/worktrees/QUAL-XXXX-slug origin/<base-branch>`
- If it already exists and isn't checked out anywhere: `git worktree add .claude/worktrees/QUAL-XXXX-slug QUAL-XXXX-slug`
- If it already exists and is already checked out somewhere: `git worktree list` to find where. If it's the main working directory, skip the worktree — run `git status` first, and if there are uncommitted changes unrelated to this ticket, stop and ask the user to clean up before starting TDD on top of stray state, otherwise work there in place with a plain `git checkout`. If it's another worktree (e.g. left over from a prior run), `EnterWorktree(path: <that worktree's path>)` and continue there instead of creating a new one — a plain `git checkout` here would just fail with "already used by worktree at '<path>'".

Load `EnterWorktree`/`ExitWorktree` via `ToolSearch("select:EnterWorktree,ExitWorktree")` if not already available. Once a worktree is created or found, call `EnterWorktree(path: ".claude/worktrees/QUAL-XXXX-slug")` to switch the session into it. Note in the final report whether a worktree was used.

The `QUAL-XXXX` identifier must lead the branch name. Never use `fix/`, `chore/`, or `bugfix/` prefixes. Example: `QUAL-1955-fix-marknoprogress-crash`.

## Step 3: Red — write failing tests

Write tests that cover the spec's **Done looks like** criteria plus the key edge cases implied by **Constraints**. Follow the project's test conventions (see CLAUDE.md).

Place test files at the appropriate path (`spec/models/`, `spec/services/`, `spec/controllers/hiring/`, etc.).

Run the tests and confirm they fail for the right reason (the behavior doesn't exist yet), not because of syntax errors or missing setup:

```bash
./bin/rspec spec/path/to/spec.rb
```

If tests fail for the wrong reason (NameError, missing factory, typo), fix the test itself before continuing.

## Step 4: Green — implement until tests pass

Work through the spec's **Change** section, respecting all **Constraints**. Read every file before editing it. Follow existing patterns in the codebase. Keep blast radius minimal — change only what the spec calls for.

Run tests after each meaningful change:

```bash
./bin/rspec spec/path/to/spec.rb
```

Iterate until green. Resist the urge to expand scope — if something tangential is broken, note it for the PR description; don't fix it here.

## Step 5: Refactor

Tests are green and are now your safety net. Re-read the implementation:

- Are there extracted helpers that would clarify the code?
- Is there duplicated logic to consolidate?
- Are names clear? Is the change localized?
- Is there dead code or commented-out scaffolding to remove?

Make the code the reviewer will be happy to read. Run tests after each refactor to confirm they stay green.

Refactoring is not optional — it is the step that makes TDD worth the overhead. Do not skip it.

## Step 6: Assess production data impact (bug fixes only)

Determine whether this is a bug fix: check the ticket labels, title, and description. If yes, and the bug has been live in production, ask: _what happened to existing data while the bug was active?_

- Identify which records were likely corrupted and write a detection query.
- Trace corrupted data through integration points — if bad data was sent to another system (webhooks, API callbacks), that system may need repair too. Analyze whether re-firing is safe (idempotent? side effects like emails/notifications/state transitions?).
- Write a remediation script in `db/scripts/` with dry-run mode defaulting to on. Follow the pattern in `db/scripts/backfill_work_simulation_id.rb`.
- If no data was affected, note that explicitly in the PR description so the reviewer knows you considered it.

Skip this step entirely for non-bug-fix tickets.

## Step 7: Commit

Split into logical commits:

1. Failing tests (red)
2. Implementation to pass tests (green)
3. Refactor (if substantive)
4. Remediation script (if Step 6 applied)

Commit messages: imperative mood, max 50 chars, capital letter, no period.

Run the project's linter/formatter before committing — see CLAUDE.md for the exact commands.

## Step 8: Open a PR

Invoke the `/create-pr` skill using the Skill tool. It opens the PR, then runs `/self-review-pr` on it: an independent review, a fix or a reasoned reply for every finding, and every review thread closed, whoever started it. Only then does the ticket move to "Needs Review". Do not move it yourself, and stay in the worktree until `/create-pr` returns, since the fixes are committed on this branch.

**Before invoking, prepare a "Decisions made" section for the PR description.** Include:

- Anything you chose that wasn't explicit in the spec (naming, minor edge cases, pattern choices)
- Any **Open Questions** from the spec that weren't resolved (flag as follow-up items, don't block the PR)
- Any genuine blockers you worked around (missing credentials, ambiguous requirements)
- Any tangential issues you noticed but didn't fix

This section is what the reviewer uses to catch silent drift from the spec. It's the single most important output of this skill.

## Step 9: Address CodeRabbit feedback

Invoke `/address-coderabbit` using the Skill tool to poll the PR just opened and handle any CodeRabbit review comments. Do this before leaving the worktree (Step 10) — `/address-coderabbit` resolves "the PR for the current branch" and pushes fix commits from the current directory, so it needs to still be running from the ticket's worktree/branch, not wherever `ExitWorktree` restores the session to.

## Step 10: Leave the worktree for follow-up

Skip this entirely if Step 2 didn't use a worktree.

Never remove the worktree automatically — an open PR isn't finished work; review comments need more commits on the same branch, and this skill has no way to know when the PR actually merges. Call `ExitWorktree(action: "keep")` to restore the session's directory, and mention the worktree path in the final report so it can be cleaned up manually (`git worktree remove <path>`) once the PR merges.

## What counts as a "genuine blocker"

A genuine blocker is something that prevents the code from functioning correctly — a missing credential, a fundamentally ambiguous requirement the spec didn't cover, an external system contract you cannot verify.

A spec whose **Today** or **Constraints** section is factually wrong about the current codebase — not just silent on an edge case — is a different failure mode than ambiguity. Flag it prominently in Decisions made regardless, and treat it as a genuine blocker (stop and ask) if the wrong premise changes what **Change** should even be.

A genuine blocker is NOT:

- A minor style preference
- A naming question — pick a name and note it in Decisions made
- An edge case the spec didn't address — use your judgment and note it
- Uncertainty about a refactoring choice — make the call and note it

Unmonitored execution means making decisions and surfacing them, not pausing on small questions.
