---
description: Self-review an open PR, address every finding, close the threads, then move the Linear ticket to Needs Review. Run after opening a PR; /create-pr does it for you.
argument-hint: [PR number]
allowed-tools: Read, Edit, Write, Grep, Glob, Skill, Monitor, ToolSearch, Bash(git *), Bash(gh api *), Bash(gh pr list *), Bash(python3 .claude/commands/scripts/pr_review_threads.py *), Bash(bundle exec *), Bash(./bin/rspec *), Bash(rubocop *), Bash(yarn *), Bash(npx *), Bash(npm *), Bash(cargo *), Bash(make *), mcp__linear-server__get_issue, mcp__linear-server__save_issue, mcp__linear-server__list_issue_statuses, mcp__claude_ai_Linear__get_issue, mcp__claude_ai_Linear__save_issue, mcp__claude_ai_Linear__list_issue_statuses
---

You are the author closing out your own PR before other people have to look at it. The ticket moves to "Needs Review" only when this is done, so that human reviewers open a PR with no clutter: nothing unaddressed, and no thread left open.

Helper for every GitHub thread operation: `python3 .claude/commands/scripts/pr_review_threads.py` (`unresolved`, `post`, `reply`, `resolve`, `gate`). Run it with no arguments for usage.

## Arguments

`/self-review-pr [PR number]`. With no number, use the PR for the current branch: `gh pr list --head "$(git branch --show-current)" --json number --jq '.[0].number'`.

## Step 1: Preconditions

Stop and say why if any of these fail; do not work around them.

- The checked-out branch is the PR's head branch (`gh api repos/{owner}/{repo}/pulls/N --jq .head.ref`).
- No modified tracked files (`git status --porcelain`; untracked files are not yours, leave them).
- `git rev-parse HEAD` equals the PR's head sha, i.e. everything is pushed.

## Step 2: Review, up to two rounds

Round 1 reviews the whole PR: invoke this repo's own review skill (e.g. `/review-changes N`, or `/code-review N` if that's what the repo has) with the Skill tool. It runs in a fresh context, so it judges the diff rather than your reasoning for it. Round 2, if there is one, reviews only what you changed in round 1: the same skill against `<sha before your fixes>..HEAD`.

Turn each finding and each `Question` into an inline comment on the PR:

- **One bold line naming the issue.** A claim, not a question or a preamble.
- **The mechanism, concretely.** What the code does, with the file, line, or symbol that makes it true.
- **Why it matters**, unless obvious.
- **The fix**, as a code block. Pick one option.
- Relaxed and informal, three short paragraphs plus a snippet at most. No em dashes, no praise.

Write them to a temp file as JSON, e.g. `[{"path": "app/foo.rb", "line": 12, "body": "**The claim.**\n\nThe mechanism."}]`, and post them as one review: `pr_review_threads.py post N <file>`. It refuses a body without a bold opener or with an em dash, anchors a line outside the diff to the nearest diffed line and names the real line in the body, and verifies the comments landed where intended. If it errors, fix the input; don't post by hand.

If the review says "No findings" and returns no questions, there is nothing to post this round.

## Step 3: Collect bot threads

CodeRabbit reviews every push, a few minutes after it lands, and each review records the commit it covers. Before triaging in each round, wait until it has reviewed the current head: poll `gh api repos/{owner}/{repo}/pulls/N/reviews --jq '[.[] | select(.user.login == "coderabbitai[bot]" and .commit_id == "<head sha>" and .state != "PENDING")] | length'` until it is above 0, for up to five minutes. Foreground `sleep` is blocked, so wait with the Monitor tool running an until-loop on that command (load it with ToolSearch if it isn't available).

If it hasn't reviewed after five minutes, carry on and say so in your report. Anything it posts after the ticket moves is left for the reviewer; re-run `/self-review-pr` to pick it up.

Its threads are handled the same way as your own, here, rather than with `/address-coderabbit`, which leaves threads open and groups its fixes.

## Step 4: Address every open thread

List them with `pr_review_threads.py unresolved N`. For each open thread, whoever started it, read the cited code first, then decide.

- **It holds:** fix it as its own commit, one commit per finding, so the history stays searchable. Write the message per `.claude/commands/shared/commit-message-style.md`. Before each commit, run the checks that apply to what you changed — see this repo's CLAUDE.md for the exact lint/type-check/test commands — and for a command or docs file, re-read it against the rest of the workflow. If no automated check applies, say what you checked by hand in the reply.
- **It doesn't, or you decline it:** no code change. The reply gives the reason, with evidence.
- **It's a question:** answer it in the reply, or fix what it exposes.

Then reply on the thread (`pr_review_threads.py reply N <comment id> <body>`, informal, says what you did and the commit sha, no em dashes) and resolve it (`pr_review_threads.py resolve <thread id>`). Push before you reply, so a sha you cite exists on the branch. Never force-push.

If you made fix commits and this was round 1, go back to Step 2 for round 2. Two rounds is the cap. Don't loop past that. If round 2 leaves something you can't settle, stop and say what it is; the gate will fail and the ticket stays where it is.

## Step 5: Gate

`pr_review_threads.py gate N` must pass: no open review thread, whoever started it. If it fails, list what's blocking and stop. Do not move the ticket.

## Step 6: Move the ticket

Take `QUAL-XXXX` from the branch name. If there isn't one, skip this step and say so. Otherwise move it to "Needs Review" (`save_issue` with `state`; `list_issue_statuses` if the name doesn't resolve), then read it back with `get_issue` to confirm.

Do not mark the PR ready for review. Agent-created PRs open as drafts, and taking one out of draft is the owner's call after checking the description against the diff and tests.

## Report

Say what you did, per thread: fixed (sha), declined (reason), or answered. Give the gate result, the ticket's new state, and anything you skipped or couldn't verify.
