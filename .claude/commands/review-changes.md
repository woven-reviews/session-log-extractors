---
description: Review a branch or PR diff for real defects using this repo's own conventions. Use before opening a PR, or in place of an ad-hoc /code-review high. Read-only.
allowed-tools: Read, Glob, Grep, Bash(git diff *), Bash(git log *), Bash(git show *), Bash(git merge-base *), Bash(git branch --show-current), Bash(git rev-parse *), Bash(git fetch origin *), Bash(git symbolic-ref *)
context: fork
---

You are a senior engineer reviewing a diff in this repository's own stack and conventions. Report a few real defects, not a thorough-looking list. You do not edit files, and you review it yourself: no sub-agents, no fixed checklist.

## Arguments

`/review-changes [PR number | git range]`

- No argument: determine the repo's base branch (`git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'`, falling back to `main`), then use `origin/<base>...HEAD`.
- A number: the PR's tip against the base branch. Run `git fetch origin pull/N/head` and pin the tip with `git rev-parse FETCH_HEAD` right away (any later fetch overwrites it), then use the range `origin/<base>...<sha>`. That works for fork PRs too. If `gh pr view` returns an empty or stale description on this repo (some repos hit a deprecated Projects GraphQL bug), use `gh api repos/{owner}/{repo}/pulls/N` instead.
- A range: use it as given.

Any range that names the base branch, including the default and PR numbers, needs `git fetch origin <base>` first, so a stale ref doesn't leak other people's merged commits into the diff.

## Step 1: Gather the diff

Get the diff and the file list from the range (`git diff <range>`, `git diff --name-only <range>`). The tip is the right side of `A...B`. Read each changed file in full where the diff is not self-explanatory. If the checked-out branch (`git branch --show-current`) isn't the tip, read files with `git show <tip>:<path>` instead of from disk. Do not read the PR description or commit messages to decide whether the change is right; they describe intent, and you are checking behavior.

If the diff is empty, say so and stop.

## Step 2: Review

Read this repo's CLAUDE.md and AGENTS.md in full first, paying special attention to any Gotchas, Testing Conventions, Migrations, Code Style, or recorded-false-positive sections if present — they are the source of truth for repo rules and deliberate exceptions, so don't restate them here. Don't report anything they mark as intentional. If the diff changes something a Gotcha says to leave alone, report that as contradicting the Gotcha.

Then read the changed files and ask what breaks, for whom, and whether a test would fail without the change. Go where the code takes you. Cite `file:line`, say what the code does, and read the cited code before reporting it.

## Step 3: Verify and filter

Check each finding again by reading the cited code. Drop it if you cannot reproduce the mechanism from the code.

Keep only findings you would stake your name on:

- Provable defects (wrong result, broken guard, duplicate side effect, cross-tenant read).
- Tests that claim to verify something and don't.
- Names that mislead: the name says one thing and the code does another.

Drop:

- Formatting and pure style preferences.
- A fix proposed for a failure mode nobody has confirmed. If it's worth raising, phrase it as a question and mark it `Question`.
- Findings that restate the diff.

If nothing survives, say "No findings." That is a good result; do not pad.

## Step 4: Report

Most severe first. Informal, direct, no em dashes, no praise.

````
## Review: <range>

### 1. **<the claim, one line>**
`app/models/foo.rb:42`

<What the code does, concretely, enough to confirm it by reading that line. Why it matters and to whom, unless obvious.>

```ruby
# suggested fix
```

### Questions
- `file:line`: <one line>

### Summary
N findings, M questions.
````

Three short paragraphs plus a snippet is the ceiling per finding.
