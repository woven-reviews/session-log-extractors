---
description: Create a pull request following the repo's PR template and Linear workflow, then self-review it. Use when ready to ship a branch.
allowed-tools: Skill, Bash(git *), Bash(gh pr create), Bash(gh pr view), Bash(gh api *), mcp__claude_ai_Linear__get_issue, mcp__claude_ai_Linear__save_issue, mcp__claude_ai_Linear__get_user, mcp__claude_ai_Linear__list_issue_statuses
context: fork
---

You are creating a pull request for the current branch, following the project's conventions.

## Reference repo guard

Before doing anything else, check if the current repo is a reference repo:

1. Run `git remote get-url --push origin` — if the output is `DISABLED`, this
   is a reference repo. **Stop immediately** and tell the user:
   > "This is a reference repo. Our team does not own it and we never create
   > PRs here. No action taken."
2. Also check the monorepo's `scripts/repo` file — if the current repo's name
   appears in `REFERENCE_REPOS`, same rule applies.

## Pre-flight checks

0. Determine the PR base branch: `git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'`. Fall back to `main` if that fails. Use this as `BASE_BRANCH` throughout.
1. Run `git status` and `git log --oneline $BASE_BRANCH..HEAD` to understand what's being shipped.
2. Run `git diff $BASE_BRANCH...HEAD` (and `git diff` for unstaged changes) to see the full diff.
3. **If on `$BASE_BRANCH` with uncommitted changes and no commits ahead:**
   - Read the diff to infer what the change is about.
   - If the diff references a `QUAL-XXXX` ticket (in file paths, commit messages, or content), use that as the branch name prefix. Otherwise create a Linear ticket first (see Linear ticket section below) to get one.
   - Create and switch to a new branch: `git checkout -b QUAL-XXXX-short-slug`
   - Stage and commit the changes: `git add <specific files>` then `git commit -m "Imperative summary"`
   - Continue with the rest of this flow on the new branch.
4. **Local checks — run these before pushing:** the repo's lint, type-check, and test commands for the changed files (see that repo's CLAUDE.md for the exact commands). CI catches these too, but failing locally first is faster than waiting for a CI run.
5. Check if the branch tracks a remote and is pushed. If not, push with `-u`.
6. **CI check — after pushing, before opening the PR:**
   - Run `git fetch origin $BASE_BRANCH && git merge-base --is-ancestor origin/$BASE_BRANCH HEAD` — if it fails, rebase first: `git rebase origin/$BASE_BRANCH && git push --force-with-lease`.
   - Poll until CI completes: `gh api repos/{owner}/{repo}/actions/runs --jq '[.workflow_runs[] | select(.head_branch == "<branch>")] | sort_by(.created_at) | reverse | .[0] | {status, conclusion}'` (`{owner}/{repo}` auto-substitutes from the current repo's git context). Wait if still running. Fix if failed. If the failure is pre-existing on `$BASE_BRANCH`, rebase to pick it up.

## Linear ticket

1. Check the branch name for a `QUAL-XXXX` pattern.
2. If found, verify the `QUAL-XXXX` identifier is at the **start** of the branch name (e.g. `QUAL-1234-my-feature`). If it appears anywhere else (e.g. `fix-foo-bar-qual-1234`):

   - Derive the correct branch name: `QUAL-XXXX-<remaining-slug>`, where the slug is the descriptive part of the old name with `fix-`, `chore-`, `bugfix-` prefixes stripped, the `QUAL-XXXX` token (case-insensitive) removed, and leading/trailing hyphens trimmed. Example: `fix-foo-bar-qual-1234` → `QUAL-1234-foo-bar`.
   - Rename the branch and update the remote:

     ```bash
     git branch -m <old-name> QUAL-XXXX-<slug>
     git push origin -u QUAL-XXXX-<slug>
     git push origin --delete <old-name>
     ```

   - Continue with the renamed branch.

3. If found (including when you just created it in the uncommitted-changes flow above, or renamed above), look up the ticket with `get_issue` to get the URL/slug.
4. If **no `QUAL-XXXX` in the branch name**, create a ticket:
   - Resolve the current user from the **Linear Identity** section in `~/.claude/CLAUDE.md`. If that section is missing, fall back to `get_user("me")` and note that `~/.claude/CLAUDE.md` should be updated.
   - Title: infer from the commits (imperative mood, concise)
   - Team: Qualified
   - Assignee: current user
   - State: "In Progress"
   - Priority: 3 (Normal)
   - Description: summarize the changes from the diff
5. Link format: `[QUAL-XXXX](https://linear.app/andela/issue/QUAL-XXXX/slug)`

## PR body format

Check for a `.github/pull_request_template.md` in the repo and use it as the
structure if present. Otherwise use these sections:

- **Summary** — What changed and what can the user or system do now
- **Why** — Linear ticket link + the problem or requirement
- **Decision** — Optional: the one material trade-off, risk, or limitation that changes review. Omit when there is none.
- **Verification** — What you ran or observed: test commands, reproduction steps, visual evidence
- **Review focus** — Optional: one to three concrete questions for reviewers. Omit when there is none.

Draft only from the ticket, diff, test output, and explicit decisions. Omit local paths, agent narration, conversation history, file-by-file walkthroughs, and generated bot summaries. Match the explanation order to the PR type (bug: symptom → cause → fix → proof; feature: outcome → decision → proof; refactor: invariant → structural change → proof). Aim for 150 words; use up to 350 only when rollout, security, or a material trade-off needs explaining.

**IMPORTANT**: Never leave an empty section or write "N/A" — omit the section instead.

## Creating the PR

Agent-created PRs open as drafts. The owner verifies the description against the diff and tests, then marks the PR ready for review — this catches an agent-drafted description that doesn't match the diff before a human reviewer wastes time on it.

Use `gh pr create --draft` with a HEREDOC body:

```bash
gh pr create --draft --title "QUAL-XXXX: Short description" --body "$(cat <<'EOF'
## Summary

What changed and what can the user or system do now.

## Why

[QUAL-XXXX](https://linear.app/andela/issue/QUAL-XXXX/...) — the problem or requirement.

## Verification

How it was tested.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**PR title format:** When a `QUAL-XXXX` ticket is associated, prefix the title — `QUAL-XXXX: Short description`. Total length under 70 chars. If no ticket, use a plain imperative title.

## After creating the PR

- Verify the PR was created by running `gh pr view <number> --json number,title,state,url` (avoid bare `gh pr view` — some repos hit a deprecated Projects GraphQL field on it).
- Attach the PR URL to the Linear ticket using `save_issue` with `links: [{url: "<PR URL>", title: "GitHub PR"}]`.
- Invoke `/self-review-pr <number>` using the Skill tool. It reviews the PR, fixes and replies to every finding, closes every review thread, whoever started it, and moves the ticket to "Needs Review" only once all of that is done. Do not move the ticket yourself; until then it stays "In Progress".
- Return the PR URL to the user, along with what `/self-review-pr` reported.

## Arguments

The user may optionally pass a title: `/create-pr Fix the widget bug`

If no title is given, infer one from the commits.
