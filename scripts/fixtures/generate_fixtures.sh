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
# This script prefers driving each CLI directly over its own SDK/library --
# fewer moving parts, no extra language runtime, and it's what most users
# will actually run. Reach for an SDK (as plan mode does below) only when
# the CLI itself provides no non-interactive way to supply a decision a step
# needs.
#
# Claude Plan mode: a bare `claude -p --permission-mode plan` run has nobody
# to answer the ExitPlanMode approval prompt, so it never calls the tool at
# all -- that part is a real, confirmed dead end (see
# github.com/anthropics/claude-code/issues/3894). But ExitPlanMode's
# approval goes through the same generic canUseTool path every other tool
# permission does, and the Agent SDK's `canUseTool` callback (Python
# `claude_agent_sdk` package, not the plain CLI) IS a documented,
# non-interactive way to answer that path -- this is what the plan-mode
# capture below drives, in its own separate session (see that section for
# why it can't just be `--continue`d onto the turns above). Confirmed by a
# real run: the *approval* tool_result text does match the interactive UI's
# canned "User has approved your plan..." prefix, and is now a real fixture
# (tests/fixtures/claude/plan_session.jsonl, test_plan_approved_is_captured).
# The *rejection* side does NOT reproduce the canned UI wording, though --
# `PermissionResultDeny(message=...)` returns exactly that message string
# with no boilerplate at all, so rejection stays covered only by the
# synthetic tests (test_plan_rejected_keeps_steering_message et al.).
#
# Claude pasted images: the same kind of dead end, and this time it's
# spelled out directly rather than pieced together from issue trackers.
# code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode lists "Image
# uploads: attach images directly to messages" as a Streaming Input Mode
# benefit, and says outright that Single Message Input "does NOT support...
# direct image attachments in messages." Every `claude -p` call in this
# script, including the `--continue -p '... @path'` step below, IS single
# message input -- so that step was never going to produce a real pasted-
# image content block, independent of whether the file reference itself
# works. It's left in below as a harmless non-fatal attempt (it may still
# capture some other real thing, such as a Read-tool-mediated image), but
# the actual pasted-image capture further down drives streaming input mode
# via the SDK instead, in its own session, the same way plan mode does.
# Confirmed by a real run: this produces a genuine top-level user-message
# image content block, now a real fixture
# (tests/fixtures/claude/pasted_image_session.jsonl) -- and as a bonus, the
# assistant's own Read-tool re-read of that same image is a real capture of
# the tool-result-image case too, replacing both synthetic tests
# (test_build_events_inlines_pasted_images_as_base64 and
# test_build_events_inlines_images_returned_inside_a_tool_result).
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
# The Claude plan-mode and pasted-image steps also need the
# `claude_agent_sdk` Python package: `pip install -e '.[fixtures]'` from the
# repo root (see pyproject.toml -- it's an optional extra, not a runtime or
# test dependency, since it's only ever needed to run this script). They
# drive Claude through that SDK (canUseTool, and streaming input) instead of
# the plain `claude` CLI. Missing it just fails those two try_steps; every
# other capture is unaffected.
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
# `-p` prompt. Left in as a harmless non-fatal attempt, but per the header
# comment, `-p` is single message input, which is documented to not support
# direct image attachments in messages at all -- which also explains why an
# earlier attempt at hand-building a stream-json stdin envelope for this got
# no trace of the turn in the transcript either: the envelope wasn't the
# problem, single message input fundamentally can't carry an image however
# it's framed. See the real pasted-image capture (via streaming input mode)
# further down instead.
try_step "Claude: attached image via @path reference (same session, continued)" \
  bash -c "cd '$CLAUDE_SCRATCH' && claude --bare --model '$CLAUDE_MODEL' --dangerously-skip-permissions --continue -p '$TURN_IMAGE @$FIXTURE_PNG'"

# Plan mode: separate fresh scratch dir/session, same reasoning as the
# denial capture above -- this can't be `--continue`d onto $CLAUDE_SCRATCH
# because it isn't driven by the `claude` CLI at all. `--bare`'s isolation
# (skip project/user hooks, skills, CLAUDE.md) is reproduced here with
# setting_sources=[] since there's no CLI flag to pass through.
# permission_mode="plan" is what routes ExitPlanMode (and any file edit)
# through can_use_tool instead of auto-running it -- see the header comment
# for why that's expected to work non-interactively where plain `-p` can't.
# One session, two turns: the callback approves the first ExitPlanMode call
# and rejects the second with a message, so both
# test_plan_approved_is_captured and test_plan_rejected_keeps_steering_message
# get a real counterpart to check against, if the resulting text matches.
CLAUDE_SCRATCH_PLAN="$ROOT/claude-scratch-plan"
new_scratch_repo "$CLAUDE_SCRATCH_PLAN"
PLAN_CAPTURE_SCRIPT="$ROOT/plan_capture.py"
cat > "$PLAN_CAPTURE_SCRIPT" <<PYEOF
import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk.types import HookMatcher, PermissionResultAllow, PermissionResultDeny

# One ExitPlanMode call per turn below, in order: approve, then deny.
DECISIONS = iter(["allow", "deny"])


async def can_use_tool(tool_name, input_data, context):
    if tool_name == "ExitPlanMode":
        if next(DECISIONS) == "allow":
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Not now -- keep the current file name.")
    return PermissionResultAllow(updated_input=input_data)


# Required workaround (documented in the SDK's own user-input guide): a
# no-op PreToolUse hook is needed to keep the stream open for can_use_tool.
async def dummy_hook(input_data, tool_use_id, context):
    return {"continue_": True}


async def main():
    options = ClaudeAgentOptions(
        model="$CLAUDE_MODEL",
        cwd="$CLAUDE_SCRATCH_PLAN",
        permission_mode="plan",
        setting_sources=[],
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[dummy_hook])]},
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Propose a plan to add a divide(a, b) function with a test, "
            "but do not implement it yet."
        )
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                print("turn 1 (expect approved):", message.result)

        await client.query(
            "Propose a plan to rename calculator.py to math_ops.py, "
            "but do not implement it yet."
        )
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                print("turn 2 (expect rejected):", message.result)


asyncio.run(main())
PYEOF
try_step "Claude: plan mode approve + reject via Agent SDK canUseTool (own session)" \
  python3 "$PLAN_CAPTURE_SCRIPT"

# Pasted image: another separate session, for the same structural reason as
# plan mode -- this needs streaming input mode (an async message generator),
# which isn't something `claude -p` flags can express. No canUseTool/hooks
# needed here (nothing to approve), so bypassPermissions keeps this step
# simple -- same effective isolation as --dangerously-skip-permissions
# elsewhere in this script.
CLAUDE_SCRATCH_IMAGE="$ROOT/claude-scratch-image"
new_scratch_repo "$CLAUDE_SCRATCH_IMAGE"
IMAGE_CAPTURE_SCRIPT="$ROOT/image_capture.py"
cat > "$IMAGE_CAPTURE_SCRIPT" <<PYEOF
import asyncio
import base64

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage


async def message_generator():
    with open("$FIXTURE_PNG", "rb") as f:
        image_data = base64.b64encode(f.read()).decode()

    # Mirrors a real interactive paste: an image content block sitting
    # directly in the user message, not a tool result or a file reference.
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "$TURN_IMAGE"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_data,
                    },
                },
            ],
        },
    }


async def main():
    options = ClaudeAgentOptions(
        model="$CLAUDE_MODEL",
        cwd="$CLAUDE_SCRATCH_IMAGE",
        permission_mode="bypassPermissions",
        setting_sources=[],
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(message_generator())
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                print("pasted-image turn result:", message.result)


asyncio.run(main())
PYEOF
try_step "Claude: pasted image via Agent SDK streaming input (own session)" \
  python3 "$IMAGE_CAPTURE_SCRIPT"

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
