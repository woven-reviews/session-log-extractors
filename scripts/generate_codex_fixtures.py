#!/usr/bin/env python3
"""Regenerate sanitized native Codex logs with a logged-in CLI.

Run manually: python3 scripts/generate_codex_fixtures.py [--interactive]
Tests read committed files and never invoke this script's main().
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
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
    "image": "Describe the attached 64-by-64 test image briefly.",
    "model": "Reply with FIRST_MODEL_FIXTURE_MARKER.",
}
INTERACTIVE_PROMPTS = {
    "plan": "Make a two-step plan for creating fixture.txt. Do not execute it. Finish with the plan.",
    "question": (
        "Before making a plan, use request_user_input to ask which of two teams "
        "should own fixture.txt: QUAL or SE. Wait for my answer, then summarize it."
    ),
    "denial": (
        "Use the shell tool to write DENIAL_FIXTURE_MARKER to fixture.txt in this "
        "temporary workspace. Request approval if the read-only sandbox requires it. "
        "If declined, stop and explain briefly."
    ),
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
            mode = payload.get("collaboration_mode")
            if isinstance(mode, dict):
                payload["collaboration_mode"] = {"mode": mode.get("mode")}
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
                if payload.get("item", {}).get("type") not in ("FileChange", "Plan"):
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
    denied_ids = {
        row["payload"].get("call_id")
        for row in rows
        if row["payload"].get("type") == "custom_tool_call_output"
        and "rejected by user" in str(row["payload"].get("output", ""))
    } if name == "denial" else set()
    for row in rows:
        payload = row["payload"]
        kind = payload.get("type")
        if kind in ("custom_tool_call", "custom_tool_call_output") and (
            name != "tool_patch_large"
            and (name != "denial" or payload.get("call_id") not in denied_ids)
        ):
            continue
        if kind == "item_completed" and name != "tool_patch_large" and not (
            name == "plan" and payload.get("item", {}).get("type") == "Plan"
        ):
            continue
        if name == "tool_patch_large" and kind == "custom_tool_call_output":
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
        if not any(
            block.get("type") == "input_image"
            for payload in payloads for block in payload.get("content", [])
            if isinstance(block, dict)
        ):
            raise RuntimeError("image: missing native image payload")
    elif name == "model":
        models = {payload.get("model") for payload in payloads if "model" in payload}
        if len(models) < 2:
            raise RuntimeError("model: resume did not switch models")
    elif name == "plan":
        if not any(p.get("collaboration_mode_kind") == "plan" for p in payloads) or not any(
            p.get("item", {}).get("type") == "Plan" for p in payloads
        ):
            raise RuntimeError("plan: missing native plan")
    elif name == "question":
        if not any(p.get("name") == "request_user_input" for p in payloads) or not any(
            "QUAL" in str(p.get("output", "")) for p in payloads
        ):
            raise RuntimeError("question: missing answered prompt")
    elif name == "denial":
        if not any(p.get("type") == "custom_tool_call" for p in payloads) or not any(
            "rejected by user" in str(p.get("output", "")) for p in payloads
        ):
            raise RuntimeError("denial: missing rejected tool call")


def capture(name: str, prompt: str, workspace: Path, image: Path | None = None) -> list[dict]:
    args = [
        "codex", "exec", "-C", str(workspace), "--skip-git-repo-check",
        "--ignore-user-config", "--sandbox", "workspace-write", "--json",
    ]
    if name == "model":
        args += ["-m", "gpt-6.1-sol"]
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
    if name == "model":
        resumed = subprocess.run(
            ["codex", "-C", str(workspace), "-s", "workspace-write", "exec",
             "--skip-git-repo-check", "resume", "-m", "gpt-6-luna", "--json",
             started[0], "Reply with SECOND_MODEL_FIXTURE_MARKER."],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        if resumed.returncode:
            raise RuntimeError(f"model: Codex resume exited {resumed.returncode}: {resumed.stderr.strip()}")
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


def capture_interactive(name: str, workspace: Path) -> list[dict]:
    """Drive a real interactive turn and read its saved native rollout."""
    process = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    received: queue.Queue = queue.Queue()

    def read_events() -> None:
        for line in process.stdout:
            try:
                received.put(json.loads(line))
            except json.JSONDecodeError as exc:
                received.put({"error": str(exc)})
        received.put(None)

    threading.Thread(target=read_events, daemon=True).start()

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def wait_for(expected, timeout: int = 120) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = received.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError(f"{name}: app-server timed out") from exc
            if event is None:
                raise RuntimeError(f"{name}: app-server exited before completing")
            if "error" in event:
                raise RuntimeError(f"{name}: app-server error: {event['error']}")
            method = event.get("method")
            if method == "item/tool/requestUserInput":
                if name != "question":
                    raise RuntimeError(f"{name}: unexpected user input request")
                question_id = event["params"]["questions"][0]["id"]
                send({"id": event["id"], "result": {
                    "answers": {question_id: {"answers": ["QUAL"]}}
                }})
            elif method == "item/commandExecution/requestApproval":
                if name != "denial":
                    raise RuntimeError(f"{name}: unexpected approval request")
                send({"id": event["id"], "result": {"decision": "decline"}})
            elif method and method.endswith("requestApproval"):
                raise RuntimeError(f"{name}: unexpected approval request: {method}")
            if expected(event):
                return event

    try:
        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "codex_fixture_generator", "title": "Codex fixtures", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }})
        wait_for(lambda event: event.get("id") == 1, 30)
        send({"method": "initialized", "params": {}})
        send({"method": "thread/start", "id": 2, "params": {
            "cwd": str(workspace), "model": "gpt-6.1-sol", "ephemeral": False,
            "approvalPolicy": "on-request" if name == "denial" else "never",
            "approvalsReviewer": "user",
            "sandbox": "read-only" if name == "denial" else "workspace-write",
        }})
        thread_id = wait_for(lambda event: event.get("id") == 2, 30)["result"]["thread"]["id"]
        send({"method": "turn/start", "id": 3, "params": {
            "threadId": thread_id,
            "input": [{"type": "text", "text": INTERACTIVE_PROMPTS[name]}],
            "cwd": str(workspace),
            "collaborationMode": {"mode": "default" if name == "denial" else "plan",
                                  "settings": {"model": "gpt-6.1-sol", "developer_instructions": None}},
        }})
        wait_for(lambda event: event.get("id") == 3, 30)
        completed = wait_for(lambda event: event.get("method") == "turn/completed")
        if completed.get("params", {}).get("turn", {}).get("status") != "completed":
            raise RuntimeError(f"{name}: turn did not complete")
        matches = list(SESSIONS.rglob(f"*{thread_id}.jsonl"))
        if len(matches) != 1:
            raise RuntimeError(f"{name}: expected one native transcript, found {len(matches)}")
        entries = [json.loads(line) for line in matches[0].read_text().splitlines()]
        rows = clean(select(name, entries), workspace)
        validate(name, rows)
        return rows
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-fixture-") as temporary, \
            tempfile.TemporaryDirectory(prefix="codex-fixture-stage-", dir=ROOT.parent) as staging:
        workspace = Path(temporary)
        stage = Path(staging)
        image = ROOT / "pixel.png"
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


def interactive_main() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-interactive-fixture-") as temporary, \
            tempfile.TemporaryDirectory(prefix="codex-fixture-stage-", dir=ROOT.parent) as staging:
        for name in INTERACTIVE_PROMPTS:
            workspace = Path(temporary) / name
            workspace.mkdir()
            rows = capture_interactive(name, workspace)
            (Path(staging) / f"{name}.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
            )
        for name in INTERACTIVE_PROMPTS:
            (Path(staging) / f"{name}.jsonl").replace(ROOT / f"{name}.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    interactive_main() if args.interactive else main()
