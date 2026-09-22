---
description: Poll a PR for CodeRabbit review comments and address the valid ones. Reusable standalone — call after opening/updating any PR, not just from /work-ticket.
allowed-tools: Bash(gh pr *), Bash(gh api *), Read, Edit, Grep, Glob, Bash(git *)
context: fork
---

You are waiting for CodeRabbit to finish reviewing a PR, then triaging its comments.

## Reference repo guard

Before doing anything else, check if the current repo is a reference repo:

1. Run `git remote get-url --push origin` — if the output is `DISABLED`, this
   is a reference repo. **Stop immediately** and tell the user:
   > "This is a reference repo. Our team does not own it and we never reply
   > to or commit against PRs here. No action taken."
2. Also check the monorepo's `scripts/repo` file — if the current repo's name
   appears in `REFERENCE_REPOS`, same rule applies.

## Arguments

`/address-coderabbit [PR-number-or-url]` — optional. Defaults to the PR for the current branch.

Requires `gh` and `jq` on `PATH`.

## Step 1: Resolve the PR

If a PR number or URL was given, use it: `gh pr view --json number,url,headRefName,headRefOid <that value>`. Otherwise, resolve the PR for the current branch: `gh pr view --json number,url,headRefName,headRefOid`.

If this isn't a repo with a CodeRabbit integration (no `.coderabbit.yaml` and no prior CodeRabbit comments in `gh pr view --json comments`), stop and tell the user CodeRabbit doesn't appear to be configured here.

## Step 2: Poll for CodeRabbit's review

CodeRabbit usually finishes within a few minutes of the PR being opened or pushed to. Poll rather than assume it's instant, but don't wait forever. Match against the current head commit (`headRefOid` from Step 1) so a stale review from before the latest push doesn't short-circuit the loop:

```bash
set -o pipefail
for i in $(seq 1 15); do
  REVIEWED=$(gh api repos/{owner}/{repo}/pulls/$PR/reviews | jq --arg head "$HEAD_SHA" '[.[] | select(.user.login=="coderabbitai[bot]" and .commit_id==$head and .state!="PENDING")] | length') || { echo "gh api/jq pipeline failed — check gh auth and jq availability" >&2; break; }
  [ "$REVIEWED" -gt 0 ] && break
  sleep 30
done
```

(`gh api` auto-substitutes the literal `{owner}/{repo}` placeholders from the current repo's git context, as used elsewhere in this file — leave them as-is. `$PR` is from Step 1, and `$HEAD_SHA` is the `headRefOid` from Step 1. Run this with a Bash timeout of at least 480000ms — the loop itself can take up to ~7.5 minutes.)

If the pipeline failed, tell the user why (auth, rate limit, missing `jq`, etc.) rather than reporting a plain "not reviewed yet." If it ran cleanly and nothing showed up after the loop, tell the user CodeRabbit hasn't reviewed yet and stop — don't fabricate a review or guess at findings.

## Step 3: Gather the comments

Two kinds matter:

```bash
set -o pipefail
gh api --paginate repos/{owner}/{repo}/pulls/$PR/comments | jq --arg head "$HEAD_SHA" '[.[] | select(.user.login=="coderabbitai[bot]" and (.original_commit_id==$head or .commit_id==$head))]' || { echo "gh api/jq pipeline failed — check gh auth and jq availability" >&2; exit 1; }
```

Filtering by `$HEAD_SHA` (the same head commit matched in Step 2) excludes findings anchored to an earlier push. These are the inline, line-anchored suggestions — the actionable ones. Ignore the single summary/walkthrough issue-comment CodeRabbit posts (`gh api repos/{owner}/{repo}/issues/$PR/comments`) — it's a recap, not a review finding.

## Step 4: Triage each comment

For each inline comment, read the file at the referenced line before deciding anything:

- **Valid** — fix it, in a normal commit. Group related fixes; don't make one commit per comment.
- **Nitpick CodeRabbit flagged as optional** (marked `⚠️ Potential issue` vs `🧹 Nitpick` in the comment body) — use judgment; skip pure style nits that don't improve correctness or readability.
- **Wrong or not applicable** — don't change code. Note why in your reply instead.

Don't expand scope beyond what each comment actually flags.

## Step 5: Reply and push

For every comment you evaluated, reply on its thread so the loop is visible to a human reviewer:

```bash
gh api repos/{owner}/{repo}/pulls/$PR/comments/{comment_id}/replies -f body="Fixed in <sha>." 
# or, for a rejected suggestion:
gh api repos/{owner}/{repo}/pulls/$PR/comments/{comment_id}/replies -f body="Not changing this because ..."
```

Push any fix commits:

```bash
git push
```

## Step 6: Summary

Tell the user how many CodeRabbit comments came in, how many you fixed, and how many you declined (with the one-line reason for each decline). Don't resolve review threads yourself — leave that for the human reviewer.
