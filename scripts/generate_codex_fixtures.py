#!/usr/bin/env python3
"""Regenerate sanitized Codex logs with a logged-in headless CLI.

Run manually: python3 scripts/generate_codex_fixtures.py
Tests read committed files and never invoke this script's main().
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "fixtures" / "codex"
SESSIONS = Path.home() / ".codex" / "sessions"
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PROMPTS = {
    "tool_patch_large": (
        "Use the shell tool to run printf TOOL_FIXTURE_MARKER. "
        "Use apply_patch to add fixture.txt with PATCH_FIXTURE_MARKER. "
        "Then use the shell tool to print fixture.txt 30 times."
    ),
    "slash": "/review fixture",
    "image": "Describe the attached one-pixel image briefly.",
}


def clean(entries: list[dict], workspace: Path) -> list[dict]:
    """Retain native extractor fields, excluding account and startup instructions."""
    kept = []
    for entry in entries:
        kind = entry.get("type")
        payload = entry.get("payload", {})
        if kind == "session_meta":
            payload = {key: payload[key] for key in ("id", "cwd") if key in payload}
        elif kind == "turn_context":
            payload = {key: payload[key] for key in ("cwd", "model", "collaboration_mode") if key in payload}
        elif kind == "response_item":
            if payload.get("type") == "message":
                if payload.get("role") not in ("user", "assistant"):
                    continue
                if payload.get("role") == "user" and "# AGENTS.md instructions" in str(payload.get("content")):
                    continue
                payload = {key: payload[key] for key in ("type", "role", "content") if key in payload}
            elif payload.get("type") in ("function_call", "custom_tool_call"):
                payload = {key: payload[key] for key in ("type", "name", "call_id", "arguments", "input", "status") if key in payload}
            elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                payload = {key: payload[key] for key in ("type", "call_id", "output") if key in payload}
            else:
                continue
        elif kind == "event_msg":
            if payload.get("type") == "item_completed":
                if payload.get("item", {}).get("type") != "FileChange":
                    continue
                payload = {"type": "item_completed", "item": payload["item"]}
            elif payload.get("type") not in ("user_message", "patch_apply_end", "task_started"):
                continue
            else:
                payload = {key: payload[key] for key in ("type", "message", "images", "local_images", "changes", "collaboration_mode_kind") if key in payload}
        else:
            continue
        kept.append({"timestamp": entry.get("timestamp"), "type": kind, "payload": payload})

    raw = json.dumps(kept, ensure_ascii=False)
    for path in sorted({str(workspace), str(workspace.resolve())}, key=len, reverse=True):
        raw = raw.replace(path, "/workspace")
    raw = raw.replace(str(Path.home()), "/home/fixture-user")
    raw = EMAIL.sub("fixture@example.invalid", raw)
    if any(value in raw for value in (str(workspace), str(Path.home()), "/var/folders/", "/private/var/")):
        raise RuntimeError("private path remains after sanitizing")
    return json.loads(raw)


def select(name: str, rows: list[dict]) -> list[dict]:
    """Keep only records needed to show the audited native shape."""
    chosen = []
    for row in rows:
        payload = row["payload"]
        kind = payload.get("type")
        if name != "tool_patch_large" and kind in (
            "custom_tool_call", "custom_tool_call_output", "item_completed"
        ):
            continue
        if kind == "custom_tool_call_output":
            payload["output"] = [
                block for block in payload["output"]
                if "FIXTURE_MARKER" in str(block) or "fixture.txt" in str(block)
            ]
        chosen.append(row)
    return chosen


def validate(name: str, rows: list[dict]) -> None:
    payloads = [row["payload"] for row in rows]
    if name == "tool_patch_large":
        if not any(p.get("type") == "custom_tool_call" for p in payloads) or not any(
            p.get("item", {}).get("type") == "FileChange" for p in payloads
        ) or not any(
            p.get("type") == "custom_tool_call_output"
            and str(p.get("output", "")).count("PATCH_FIXTURE_MARKER") >= 20
            for p in payloads
        ):
            raise RuntimeError("tool_patch_large: incomplete native tool or patch")
    elif name == "slash":
        if "/review fixture" not in json.dumps(payloads):
            raise RuntimeError("slash: missing user command")
    elif name == "image":
        if "/workspace/pixel.png" not in json.dumps(payloads):
            raise RuntimeError("image: missing native image path")


def capture(name: str, prompt: str, workspace: Path, image: Path | None = None) -> list[dict]:
    args = [
        "codex", "exec", "-C", str(workspace), "--skip-git-repo-check",
        "--ignore-user-config", "--sandbox", "workspace-write", "--json",
    ]
    if image:
        args += ["-i", str(image), "--"]
    args.append(prompt)
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{name}: Codex exited {result.returncode}: {result.stderr.strip()}")
    events = [json.loads(line) for line in result.stdout.splitlines()
              if line.startswith("{")]
    started = [event["thread_id"] for event in events
               if event.get("type") == "thread.started"]
    if len(started) != 1:
        raise RuntimeError(f"{name}: expected one Codex thread")
    matches = list(SESSIONS.rglob(f"*{started[0]}.jsonl"))
    if len(matches) != 1:
        raise RuntimeError(f"{name}: expected one native transcript, found {len(matches)}")
    entries = [json.loads(line) for line in matches[0].read_text().splitlines()]
    if not any(row.get("type") == "response_item" and
               row.get("payload", {}).get("role") == "assistant" for row in entries):
        raise RuntimeError(f"{name}: missing assistant response")
    rows = clean(select(name, entries), workspace)
    validate(name, rows)
    return rows


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-fixture-") as temporary, \
            tempfile.TemporaryDirectory(prefix="codex-fixture-stage-", dir=ROOT.parent) as staging:
        workspace = Path(temporary)
        stage = Path(staging)
        image = ROOT.parent / "copilot" / "pixel.png"
        for name, prompt in PROMPTS.items():
            work = workspace / name
            work.mkdir()
            if name == "image":
                (work / "pixel.png").write_bytes(image.read_bytes())
            rows = capture(name, prompt, work, work / "pixel.png" if name == "image" else None)
            (stage / f"{name}.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
            )
        ROOT.mkdir(exist_ok=True)
        for name in PROMPTS:
            (stage / f"{name}.jsonl").replace(ROOT / f"{name}.jsonl")


if __name__ == "__main__":
    main()
