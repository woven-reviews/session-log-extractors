#!/usr/bin/env bash
# Generates real Claude Code / Codex / Copilot session logs for QUAL-2319
# fixtures, entirely inside a throwaway /tmp root with isolated config dirs,
# so nothing real (your CLAUDE.md, hooks, MCP config, session history, real
# file paths) is ever reachable by the CLIs while they run.
#
# Beyond a basic coding task, this also makes a best-effort attempt at real
# (not synthetic-Python) coverage of: skill invocation, a real permission
# denial, and pasted/attached images -- using documented CLI flags for each.
# A few of these are confirmed-to-exist flags whose exact resulting
# transcript shape hasn't been verified (this session can't invoke these
# CLIs itself), so each experimental step is non-fatal: if one doesn't
# produce what we expect, the script keeps going and says so.
#
# Claude Plan mode is deliberately NOT attempted here: exiting plan mode is
# an ExitPlanMode tool_use that requires a human's interactive approve/
# reject decision, and there is no non-interactive/headless way to supply
# that decision. A headless `-p --permission-mode plan` run just answers
# with the plan as plain assistant text and never calls ExitPlanMode, so it
# produces nothing the extractor doesn't already cover with synthetic
# tests. Rely on those (test_plan_approved_is_captured et al.) instead.
#
# Run this in your OWN terminal (not through Claude Code) -- it invokes
# claude/codex/copilot as live agents, which this harness itself blocks a
# running session from doing to another.
#
# Credentials: config-dir isolation alone does NOT stop the CLI from writing
# a *real* login credential into the throwaway dir if you sign in with your
# personal account there. Instead, export scoped/revocable credentials below
# before running, so no personal OAuth session ever gets written to disk:
#
#   ANTHROPIC_API_KEY     - a dedicated Anthropic API key (console.anthropic.com).
#                           With --bare, Claude uses ONLY this key; it never
#                           touches OAuth or the OS keychain.
#   OPENAI_API_KEY        - a dedicated OpenAI API key (platform.openai.com).
#                           Logged into the throwaway CODEX_HOME via
#                           `codex login --with-api-key`, never your ChatGPT
#                           account's OAuth session.
#   COPILOT_GITHUB_TOKEN  - a fine-grained GitHub PAT scoped to ONLY the
#                           "Copilot Requests" permission. Copilot reads this
#                           env var itself and skips any login/OAuth flow.
#                           (Copilot generation is currently DISABLED below --
#                           no usable token yet. Flip RUN_COPILOT to true and
#                           export this once you have one.)
#
# Revoke all credentials once this script finishes -- they only need to
# exist for the few minutes this takes to run.
set -euo pipefail
# Note: try_step()'s `if "$@"; then ... else ... fi` is exempt from -e's
# exit-on-failure (commands tested in an if-condition never trigger it), so
# -e still stays strict for every other, non-experimental command below.

# Set to true and export COPILOT_GITHUB_TOKEN once a usable, scoped token
# exists. Claude and Codex generation are unaffected either way.
RUN_COPILOT=false

: "${ANTHROPIC_API_KEY:?Set a dedicated, revocable ANTHROPIC_API_KEY before running (see header comment)}"
: "${OPENAI_API_KEY:?Set a dedicated, revocable OPENAI_API_KEY before running (see header comment)}"
if [ "$RUN_COPILOT" = true ]; then
  : "${COPILOT_GITHUB_TOKEN:?Set a scoped, revocable COPILOT_GITHUB_TOKEN before running (see header comment)}"
fi

ROOT="$(mktemp -d /tmp/qual2319-fixture-gen.XXXXXX)"
echo "=== Working root: $ROOT ==="

# Run a step without letting a failure kill the whole script (several steps
# below use documented-but-unverified flag combinations). Failures are
# tracked in FAILED_STEPS so the closing summary only mentions this at all
# when something actually failed, instead of printing unconditionally.
FAILED_STEPS=()
try_step() {
  local desc="$1"
  shift
  echo "--- $desc ---"
  if "$@"; then
    echo "    ok"
  else
    local rc=$?
    echo "    ** did not succeed (rc=$rc) -- leaving this capability to the synthetic tests **"
    FAILED_STEPS+=("$desc")
  fi
}

# A hard ceiling for the one step below that runs *without*
# --dangerously-skip-permissions in a headless `-p` session: if Claude Code
# ever actually blocks on an interactive prompt instead of resolving it
# immediately (there being no human here to answer it), this stops the whole
# script hanging forever instead of just failing that one step. `timeout(1)`
# isn't on macOS by default, so this is a portable kill-after-N-seconds
# implemented with plain job control.
# ponytail: SIGTERM only, no SIGKILL escalation -- fine for a `claude` child.
run_with_timeout() {
  local secs="$1"
  shift
  ( "$@" ) &
  local pid=$!
  ( sleep "$secs" && kill -TERM "$pid" ) 2>/dev/null &
  local watcher=$!
  local rc=0
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watcher" 2>/dev/null || true
  return "$rc"
}

# A small, standard RGB PNG (4x4 solid color), built from scratch with only
# the stdlib (struct + zlib -- no Pillow dependency needed on your machine).
# The test suite's own 1x1 grayscale+alpha _PNG_B64 constant is byte-valid
# (Pillow opens it) but that minimal/unusual format got a real "unable to
# process image: invalid or unsupported image data" rejection from Codex --
# a plain small RGB image is far more likely to be universally accepted.
FIXTURE_PNG="$ROOT/fixture.png"
python3 -c "
import struct, zlib

def chunk(ctype, data):
    return (struct.pack('>I', len(data)) + ctype + data +
            struct.pack('>I', zlib.crc32(ctype + data) & 0xffffffff))

width, height, rgb = 4, 4, (200, 30, 30)
sig = b'\x89PNG\r\n\x1a\n'
ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
row = bytes([0]) + bytes(rgb) * width
idat = chunk(b'IDAT', zlib.compress(row * height, 9))
iend = chunk(b'IEND', b'')

with open('$FIXTURE_PNG', 'wb') as f:
    f.write(sig + ihdr + idat + iend)
"

# A small, entirely fabricated coding task -- deliberately not trivial:
# there's an existing function to discover, a new one to add with its own
# test, and a real test run, so the resulting transcript has to contain a
# Read, a Write/Edit, and a Bash/shell tool call, not just prose. Also seeds
# a trivial fabricated skill under every convention we found documented, so
# whichever tool looks for skills where finds one.
new_scratch_repo() {
  local dir="$1"
  mkdir -p "$dir"
  cat > "$dir/calculator.py" <<'EOF'
def subtract(a, b):
    return a - b
EOF
  cat > "$dir/test_calculator.py" <<'EOF'
from calculator import subtract


def test_subtract():
    assert subtract(5, 3) == 2


if __name__ == "__main__":
    test_subtract()
    print("ok")
EOF
  for skill_root in .claude/skills .agents/skills .github/skills; do
    mkdir -p "$dir/$skill_root/fixture-skill"
    cat > "$dir/$skill_root/fixture-skill/SKILL.md" <<'EOF'
---
name: fixture-skill
description: A trivial fabricated skill used only to exercise skill-invocation parsing in generated fixtures. Says hello and lists any *.py files in the current directory.
---

# Fixture Skill

When invoked, say a short greeting and list the Python files in the current
directory.
EOF
  done
  (
    cd "$dir"
    git init -q
    git config user.name "Fixture Bot"
    git config user.email "fixture@example.invalid"
    git add -A
    git commit -q -m "seed: calculator with subtract only, plus a fixture skill"
  )
}

TURN1="Look at the files in this repo and tell me in one sentence what's here."
TURN2="Add an add(a, b) function to calculator.py, write a small test for it \
in test_calculator.py (plain assert, no pytest needed), and run it to \
confirm it works."
TURN3="Now do the same thing for a multiply(a, b) function: implement it, \
add a test, and run it."
# Root cause of two straight failures (plain "/fixture-skill", then prose
# matching the skill's own description) found via the actual current docs
# (code.claude.com/docs/en/headless and /skills), not guessed: --bare skips
# auto-discovery of skills entirely, so the model never had "fixture-skill"
# in its catalog either way -- no phrasing could have worked. The docs also
# confirm plain "/skill-name" IS the real, documented invocation ("Include
# /skill-name in the prompt string and Claude Code expands it before
# running"), so once loading is fixed (see --add-dir below), the original
# simple phrasing is the right one -- restored here.
TURN_SKILL="/fixture-skill"
TURN_DENIAL="Please delete calculator.py by running: rm calculator.py"
# No apostrophes in these two: they get embedded inside a nested
# single-quoted string (bash -c "... '$VAR' ...") for the experimental
# steps below, where an apostrophe would prematurely close the inner quote.
TURN_IMAGE="Describe what is in the attached image, in one sentence."
# Copilot-only below (Claude's plan-mode attempt was dropped -- see the
# header comment: ExitPlanMode can't be triggered non-interactively there).
TURN_PLAN="Propose a plan to add a divide(a, b) function with a test, but do not implement it yet."

# Pinned explicitly so the generated fixtures don't silently change shape
# whenever an account's default model changes. All confirmed valid by
# grepping the actual installed binaries/packages, not guessed -- and the
# two CLIs do NOT share a model-naming scheme (Copilot's installed package
# has no gpt-5.6-* strings at all, only up to the gpt-5.4-mini/nano family).
#   Claude:  cheapest/fastest tier -- this task needs no real reasoning depth.
#   Codex:   gpt-5.6-luna -- Codex's own doc table labels this "primary
#            choice for faster or cheaper workloads" (gpt-5.6-terra is the
#            balanced/default tier one rung up).
#   Copilot: gpt-5.4-mini -- cheap tier, from Copilot's own model list.
CLAUDE_MODEL="claude-haiku-4-5-20251001"
CODEX_MODEL="gpt-5.6-luna"
COPILOT_MODEL="gpt-5.4-mini"

# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------
export CLAUDE_CONFIG_DIR="$ROOT/claude-home"
mkdir -p "$CLAUDE_CONFIG_DIR"
CLAUDE_SCRATCH="$ROOT/claude-scratch"
new_scratch_repo "$CLAUDE_SCRATCH"

# A PreToolUse hook that blocks only `rm` commands, with a custom reason --
# so the denial-turn step further down gets a genuine, non-interactive
# permission denial to capture, instead of guessing at a flag.
# --disallowedTools was tried first and confirmed NOT to work for this: it
# makes the tool unavailable entirely, so the transcript gets a hard "No
# such tool available: Bash" tool error, not the "Permission for this tool
# use was denied" wording the extractor recognizes.
#
# A real run then confirmed --dangerously-skip-permissions suppresses this
# hook too, not just the interactive prompt -- `rm` succeeded outright with
# the hook configured and no trace of it firing. So this hook is only
# actually exercised by the separate, bypass-free session set up below (see
# CLAUDE_SCRATCH_DENIAL); it stays configured here, in this shared
# CLAUDE_CONFIG_DIR, for both sessions, since it's harmless (matches only
# `rm`) and every other Claude step here still runs with the bypass on.
#
# Docs confirm a hook can block a tool via exit code 2 with the reason on
# stderr ("If blocked (exit 2): Claude sees the block reason"), but do NOT
# specify whether the resulting tool_result reuses the exact same canned
# wording an interactive human denial produces -- hence this is still a
# try_step, not a guaranteed-good capture.
DENY_RM_HOOK="$CLAUDE_CONFIG_DIR/deny-rm.sh"
cat > "$DENY_RM_HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
set -euo pipefail
command="$(python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))")"
if [[ "$command" == *rm\ * ]]; then
  echo "We need a new branch for this" >&2
  exit 2
fi
exit 0
HOOK_EOF
chmod +x "$DENY_RM_HOOK"
cat > "$CLAUDE_CONFIG_DIR/settings.json" <<SETTINGS_EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "$DENY_RM_HOOK"}]
      }
    ]
  }
}
SETTINGS_EOF

echo "--- Claude: turn 1 (--bare forces API-key auth; OAuth/keychain never read) ---"
(cd "$CLAUDE_SCRATCH" && claude --bare --model "$CLAUDE_MODEL" --dangerously-skip-permissions -p "$TURN1")

echo "--- Claude: turn 2 (same session, continued) ---"
(cd "$CLAUDE_SCRATCH" && claude --bare --model "$CLAUDE_MODEL" --dangerously-skip-permissions --continue -p "$TURN2")

echo "--- Claude: turn 3 (same session, continued) ---"
(cd "$CLAUDE_SCRATCH" && claude --bare --model "$CLAUDE_MODEL" --dangerously-skip-permissions --continue -p "$TURN3")

# --add-dir is the documented exception to --bare's skill-loading skip:
# "A directory you name with --add-dir is a partial exception: bare mode
# loads skills from its .claude/skills/ folder" (code.claude.com/docs/en/
# headless). Passed even though we're already cd'd into $CLAUDE_SCRATCH --
# bare mode does not auto-scan cwd's .claude/skills/ on its own; naming the
# same directory via --add-dir is what turns the exception on. Only
# .claude/skills/ is the real, documented Claude Code convention -- the
# .agents/skills/ and .github/skills/ copies new_scratch_repo also seeds
# are for Codex/Copilot's own conventions, not this one.
try_step "Claude: skill invocation via --add-dir (documented --bare exception; same session, continued)" \
  bash -c "cd '$CLAUDE_SCRATCH' && claude --bare --model '$CLAUDE_MODEL' --dangerously-skip-permissions --continue --add-dir '$CLAUDE_SCRATCH' -p '$TURN_SKILL'"

# Confirmed by a real run: --dangerously-skip-permissions suppresses hooks
# too, not just the interactive prompt -- the PreToolUse hook above never
# fired and `rm` just succeeded outright when tried in-session like the
# other steps. So this one deliberately runs in a SEPARATE, fresh session
# (fresh scratch dir, no --continue) with the bypass flag dropped -- the
# untested baseline hypothesis from the start: headless -p has no human to
# answer a permission prompt, so it may auto-deny rather than hang. The
# PreToolUse hook is still configured in this CLAUDE_CONFIG_DIR too, so
# either mechanism firing here is a genuine capture. 30s timeout in case
# that hypothesis is wrong and it blocks waiting for an answer that will
# never come.
CLAUDE_SCRATCH_DENIAL="$ROOT/claude-scratch-denial"
new_scratch_repo "$CLAUDE_SCRATCH_DENIAL"
try_step "Claude: real permission denial, no bypass, fresh session (30s timeout)" \
  run_with_timeout 30 \
  bash -c "cd '$CLAUDE_SCRATCH_DENIAL' && claude --bare --model '$CLAUDE_MODEL' -p '$TURN_DENIAL'"

# `@path` is the documented syntax for attaching a local file to a headless
# `-p` prompt. Simpler and more likely to actually work than the previous
# attempt, which hand-built a stream-json stdin payload (a "type":"user"
# envelope with an inline base64 image block) and got no trace of the turn
# in the resulting transcript at all -- no error, no image, nothing, so
# something about that envelope/timing was wrong in a way that couldn't be
# debugged further without live access to the CLI.
try_step "Claude: attached image via @path reference (same session, continued)" \
  bash -c "cd '$CLAUDE_SCRATCH' && claude --bare --model '$CLAUDE_MODEL' --dangerously-skip-permissions --continue -p '$TURN_IMAGE @$FIXTURE_PNG'"

echo "--- Claude session file(s) ---"
find "$CLAUDE_CONFIG_DIR/projects" -name '*.jsonl'

# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
export CODEX_HOME="$ROOT/codex-home"
mkdir -p "$CODEX_HOME"
CODEX_SCRATCH="$ROOT/codex-scratch"
new_scratch_repo "$CODEX_SCRATCH"

echo "--- Codex: logging into throwaway CODEX_HOME with the dedicated API key ---"
printenv OPENAI_API_KEY | codex login --with-api-key

echo "--- Codex: turn 1 ---"
(cd "$CODEX_SCRATCH" && codex exec --model "$CODEX_MODEL" --sandbox workspace-write "$TURN1")

echo "--- Codex: turn 2 (resume same session) ---"
(cd "$CODEX_SCRATCH" && codex exec resume --last --model "$CODEX_MODEL" "$TURN2")

echo "--- Codex: turn 3 (resume same session) ---"
(cd "$CODEX_SCRATCH" && codex exec resume --last --model "$CODEX_MODEL" "$TURN3")

# Codex's slash-command detection is plain regex on the message text (unlike
# Claude's tag-based interactive dispatch), so this is a real slash-command
# fixture regardless of whether Codex's skill-discovery path (unconfirmed)
# actually picks up the seeded .agents/skills/fixture-skill/SKILL.md.
try_step "Codex: slash command / best-effort skill (resume same session)" \
  bash -c "cd '$CODEX_SCRATCH' && codex exec resume --last --model '$CODEX_MODEL' '$TURN_SKILL'"

try_step "Codex: attached image (resume same session)" \
  bash -c "cd '$CODEX_SCRATCH' && codex exec resume --last --model '$CODEX_MODEL' --image '$FIXTURE_PNG' '$TURN_IMAGE'"

# Best-effort: a read-only sandbox denying a write is a real execution
# failure, but it may not match the extractor's specific permission-denial
# regex (that format looks tied to Codex-as-subagent usage). Real content
# either way -- worth checking once generated rather than assuming.
try_step "Codex: best-effort denial via read-only sandbox (separate session)" \
  bash -c "cd '$CODEX_SCRATCH' && codex exec --model '$CODEX_MODEL' --sandbox read-only 'Delete calculator.py by running: rm calculator.py'"

echo "--- Codex session file(s) ---"
find "$CODEX_HOME/sessions" -name '*.jsonl'

# ---------------------------------------------------------------------------
# Copilot -- two separate scratch repos, so there's more than one real
# session to choose from if the fixtures end up needing that.
#
# DISABLED for now (no usable Copilot token) -- flip RUN_COPILOT to true
# above and export COPILOT_GITHUB_TOKEN once you have one scoped just to
# "Copilot Requests". Claude and Codex fixtures are unaffected by this.
# ---------------------------------------------------------------------------
if [ "$RUN_COPILOT" != true ]; then
  echo "--- Copilot: skipped (RUN_COPILOT=false) ---"
else

export COPILOT_HOME="$ROOT/copilot-home"
mkdir -p "$COPILOT_HOME"

for i in 1 2; do
  COPILOT_SCRATCH="$ROOT/copilot-scratch-$i"
  new_scratch_repo "$COPILOT_SCRATCH"

  echo "--- Copilot: session $i, turn 1 (COPILOT_GITHUB_TOKEN auths automatically) ---"
  (cd "$COPILOT_SCRATCH" && copilot -p "$TURN1" --model "$COPILOT_MODEL" --allow-all-tools)

  echo "--- Copilot: session $i, turn 2 (continued) ---"
  (cd "$COPILOT_SCRATCH" && copilot -p "$TURN2" --model "$COPILOT_MODEL" --allow-all-tools --continue)

  try_step "Copilot: session $i skill invocation (continued)" \
    bash -c "cd '$COPILOT_SCRATCH' && copilot -p '$TURN_SKILL' --model '$COPILOT_MODEL' --allow-all-tools --continue"

  try_step "Copilot: session $i real permission denial (deny just rm, allow everything else)" \
    bash -c "cd '$COPILOT_SCRATCH' && copilot -p '$TURN_DENIAL' --model '$COPILOT_MODEL' --allow-all-tools --deny-tool 'shell(rm*)' --continue"

  try_step "Copilot: session $i attached image (continued)" \
    bash -c "cd '$COPILOT_SCRATCH' && copilot -p '$TURN_IMAGE' --model '$COPILOT_MODEL' --allow-all-tools --attachment '$FIXTURE_PNG' --continue"

  try_step "Copilot: session $i plan mode (documented --plan + --mode autopilot auto-approves and implements)" \
    bash -c "cd '$COPILOT_SCRATCH' && copilot -p '$TURN_PLAN' --model '$COPILOT_MODEL' --plan --mode autopilot --allow-all-tools --continue"
done

echo "--- Copilot session files ---"
find "$COPILOT_HOME" -maxdepth 4

fi  # RUN_COPILOT

# ---------------------------------------------------------------------------
# Immediate sanity check -- same secret/PII sweep used to clear the fixtures
# earlier. Defense-in-depth, not the primary defense (isolation above is).
# ---------------------------------------------------------------------------
echo
echo "=== Sanity grep for secret/PII patterns across everything generated ==="
if grep -rEni '(sk-[a-z0-9]{10,}|ghp_[a-z0-9]{10,}|aws_secret|api[_-]?key|password|token"\s*:\s*"[^"]{10,}|BEGIN [A-Z]+ PRIVATE KEY|/Users/[a-zA-Z]+|@andela\.com|@gmail\.com|@[a-zA-Z0-9.-]+\.[a-z]{2,}\b)' \
  "$CLAUDE_CONFIG_DIR/projects" "$CODEX_HOME/sessions" "${COPILOT_HOME:-/nonexistent}" 2>/dev/null; then
  echo "^^^ grep found matches above -- check them before handing off"
else
  echo "(clean -- no matches)"
fi

echo
echo "=== Done. Everything lives under: $ROOT ==="
if [ "$RUN_COPILOT" != true ]; then
  echo "Copilot was skipped (no usable token yet) -- only Claude and Codex"
  echo "fixtures were regenerated this run. Copilot's fixture stays as-is"
  echo "until you have a token and re-run with RUN_COPILOT=true."
fi
if [ "${#FAILED_STEPS[@]}" -gt 0 ]; then
  echo "${#FAILED_STEPS[@]} experimental step(s) did not succeed (that's fine --"
  echo "each one just leaves that capability covered by the synthetic tests):"
  for s in "${FAILED_STEPS[@]}"; do
    echo "  - $s"
  done
else
  echo "Every step, including the experimental ones, succeeded."
fi
echo "Tell Claude Code that path (or just that you're done -- it's the newest"
echo "/tmp/qual2319-fixture-gen.* directory) and it'll take it from here:"
echo "inspect what actually got captured, copy the real files into"
echo "tests/fixtures/, update the tests to match, and re-run its own secret"
echo "scan before committing."
