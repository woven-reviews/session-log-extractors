#!/usr/bin/env python3
"""Manually regenerate native Claude Code fixtures with a live Claude login.

Run: python3 scripts/generate_claude_fixtures.py
Captured with Claude Code 2.1.292. Tests never run this script. Each session
uses a temporary project, project-only settings, and no MCP integrations.
Claude's existing login remains in the user's config because an empty
CLAUDE_CONFIG_DIR cannot access this machine's OAuth login.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "fixtures" / "claude"
PROJECTS = Path.home() / ".claude" / "projects"
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def clean(entries: list[dict], workspace: Path) -> str:
    entries = [entry for entry in entries if entry.get("type") in {"user", "assistant"}]
    for entry in entries:
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            entry["message"]["content"] = [
                block for block in content
                if not isinstance(block, dict) or block.get("type") != "thinking"
            ]
    entries = [entry for entry in entries if entry.get("message", {}).get("content")]
    text = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
    text = text.replace(str(workspace.resolve()), "/workspace")
    text = text.replace(str(workspace), "/workspace")
    text = text.replace(str(Path.home()), "/home/fixture-user")
    text = EMAIL.sub("fixture@example.invalid", text)
    ids: dict[str, str] = {}
    text = UUID.sub(
        lambda match: ids.setdefault(
            match.group(), f"00000000-0000-4000-8000-{len(ids) + 1:012d}"
        ),
        text,
    )
    if any(path in text for path in (str(workspace), str(Path.home()), "/private/var/")):
        raise RuntimeError("private path remains after sanitizing")
    return text + "\n"


def run_case(name: str, prompt: str, *, tools: str = "") -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="claude-fixture-") as temporary:
        workspace = Path(temporary)
        settings = workspace / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}\n")
        output: dict[str, str] = {}
        if name == "skill_command":
            skill = workspace / ".claude/skills/fixture-skill/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: fixture-skill\ndescription: Answer with a fixture marker.\n"
                "---\n\nSay SKILL_FIXTURE_MARKER.\n"
            )
            command = workspace / ".claude/commands/fixture-command.md"
            command.parent.mkdir(parents=True)
            command.write_text("Say COMMAND_FIXTURE_MARKER.\n")
            output["project/.claude/skills/fixture-skill/SKILL.md"] = skill.read_text()
            output["project/.claude/commands/fixture-command.md"] = command.read_text()
        if name == "large_result":
            (workspace / "large.txt").write_text("fixture line\n" * 800)
        if name == "continued":
            (workspace / "context.txt").write_text(
                "Launch codename: BLUE-WREN-17\nFallback codename: RED-FOX-23\n"
            )

        session_id = str(uuid.uuid4())

        def invoke(message: str, model: str | None, resume: bool = False) -> None:
            args = ["claude", "--print", message, "--output-format", "json"]
            if model:
                args.extend(["--model", model])
            args.extend([
                "--resume" if resume else "--session-id", session_id,
                "--max-budget-usd", "0.25", "--permission-mode", "dontAsk",
                "--permission-prompts", "none", "--setting-sources", "project",
                "--strict-mcp-config", "--no-chrome",
            ])
            if tools:
                args.extend(["--tools", tools])
            print(f"{name}: {shlex.join(args)}", flush=True)
            result = subprocess.run(
                args, cwd=workspace, stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
            )
            if result.returncode:
                raise RuntimeError(
                    f"{name}: Claude exited {result.returncode}: {result.stderr.strip()}"
                )

        invoke(prompt, "haiku")
        if name == "skill_command":
            invoke("/fixture-command", "haiku", resume=True)
        if name == "model":
            invoke("/model sonnet", None, resume=True)
            invoke("Reply with SECOND_MODEL_FIXTURE_MARKER.", "sonnet", resume=True)
        if name == "continued":
            invoke(
                "Which codename was the fallback in your previous answer? "
                "Answer without using tools.",
                "haiku", resume=True,
            )

        matches = list(PROJECTS.rglob(f"{session_id}.jsonl"))
        if len(matches) != 1:
            raise RuntimeError(f"{name}: expected one transcript, found {len(matches)}")
        transcript = matches[0]
        entries = [json.loads(line) for line in transcript.read_text().splitlines()]
        blocks = [
            block for entry in entries
            for block in entry.get("message", {}).get("content", [])
            if isinstance(block, dict)
        ]
        calls = {block.get("name") for block in blocks if block.get("type") == "tool_use"}
        expected = {
            "skill_command": "Skill", "large_result": "Read",
            "permission_denial": "Bash", "subagent": "Agent",
            "continued": "Read",
        }
        if name in expected and expected[name] not in calls:
            raise RuntimeError(f"{name}: expected {expected[name]} tool call")
        if name == "skill_command" and not any(
            "<command-name>/fixture-command</command-name>"
            in str(entry.get("message", {}).get("content", ""))
            for entry in entries
        ):
            raise RuntimeError("skill_command: direct slash command was not recorded")
        if name == "permission_denial" and not any(
            "denied" in str(block.get("content", "")).lower()
            for block in blocks if block.get("type") == "tool_result"
        ):
            raise RuntimeError("permission_denial: no denial result")
        if name == "model":
            if not any(
                "<command-name>/model</command-name>"
                in str(entry.get("message", {}).get("content", ""))
                for entry in entries
            ):
                raise RuntimeError("model: direct /model command was not recorded")
            if len({
                entry.get("message", {}).get("model")
                for entry in entries if entry.get("type") == "assistant"
                and entry.get("message", {}).get("model") not in (None, "<synthetic>")
            }) < 2:
                raise RuntimeError("model: resume did not produce two model IDs")
        if not any(entry.get("type") == "assistant" for entry in entries):
            raise RuntimeError(f"{name}: transcript has no assistant message")
        if name == "continued":
            answers = [
                block.get("text", "") for entry in entries
                if entry.get("type") == "assistant"
                for block in entry.get("message", {}).get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if len(answers) < 2 or not all("RED-FOX-23" in answer for answer in answers[-2:]):
                raise RuntimeError("continued: follow-up did not retain the fallback codename")
        output[f"{name}.jsonl"] = clean(entries, workspace)

        if name == "subagent":
            sidecars = transcript.parent / session_id / "subagents"
            agents = list(sidecars.glob("agent-*.jsonl"))
            if len(agents) != 1:
                raise RuntimeError(f"subagent: expected one sidecar, found {len(agents)}")
            agent = agents[0]
            meta = agent.with_suffix(".meta.json")
            if not meta.is_file():
                raise RuntimeError("subagent: missing native metadata sidecar")
            agent_entries = [json.loads(line) for line in agent.read_text().splitlines()]
            output["subagent/subagents/agent-fixture.jsonl"] = clean(
                agent_entries, workspace
            )
            output["subagent/subagents/agent-fixture.meta.json"] = (
                json.dumps(json.loads(meta.read_text()), ensure_ascii=False) + "\n"
            )
        return output


def main() -> None:
    files: dict[str, str] = {}
    cases = [
        ("baseline", "Reply with BASELINE_FIXTURE_MARKER and nothing else.", ""),
        ("skill_command", "Invoke /fixture-skill and report its marker.", ""),
        ("large_result", "Use Read on large.txt, then report its line count.", "Read"),
        (
            "permission_denial",
            "Use Bash to write the word fixture to denied.txt. Report the permission result.",
            "Bash",
        ),
        ("subagent", "Use the Agent tool to ask a subagent what 2 + 2 is.", "Agent"),
        ("model", "Reply with FIRST_MODEL_FIXTURE_MARKER.", ""),
        (
            "continued",
            "Read context.txt, then tell me the launch and fallback codenames.",
            "Read",
        ),
    ]
    for name, prompt, tools in cases:
        files.update(run_case(name, prompt, tools=tools))
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="claude-fixture-staging-", dir=ROOT.parent) as stage:
        for name, content in files.items():
            path = Path(stage) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for name in files:
            destination = ROOT / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            (Path(stage) / name).replace(destination)


if __name__ == "__main__":
    main()
