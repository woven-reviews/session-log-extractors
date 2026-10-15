#!/usr/bin/env python3
"""Regenerate sanitized native Copilot fixtures with an existing Copilot login.

Run manually: python3 scripts/generate_copilot_fixtures.py
Tests never invoke Copilot or read the operator's local state.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "fixtures" / "copilot"
STORE = Path.home() / ".copilot" / "session-store.db"
STATE = Path.home() / ".copilot" / "session-state"
TABLES = (
    "sessions", "turns", "checkpoints", "session_files", "session_refs",
    "assistant_usage_events",
)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/ax6fN8A"
    "AAAASUVORK5CYII="
)


def clean_text(value: str, workspace: Path) -> str:
    for path in sorted({str(workspace), str(workspace.resolve())}, key=len, reverse=True):
        value = value.replace(path, "/workspace")
    value = value.replace(str(Path.home()), "/home/fixture-user")
    value = EMAIL.sub("fixture@example.invalid", value)
    if any(part in value for part in (str(Path.home()), str(workspace),
                                      "/var/folders/", "/private/var/")):
        raise RuntimeError("private path remains after sanitizing")
    return value


def clean_event_lines(lines: list[str], workspace: Path) -> str:
    cleaned = []
    for line in lines:
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise RuntimeError("unexpected Copilot event format")
        if event["type"] in ("session.usage_checkpoint", "system.message"):
            continue  # Account diagnostics and system prompts do not describe a session turn.
        cleaned.append(clean_text(json.dumps(event, ensure_ascii=False), workspace))
    return "\n".join(cleaned) + "\n"


def copy_session(source: Path, session_id: str, destination: Path, workspace: Path) -> None:
    """Copy one session's native rows and table definitions, excluding other sessions."""
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        if src.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise RuntimeError(f"missing native session {session_id}")
        dst.execute("PRAGMA foreign_keys=OFF")
        for table in TABLES:
            schema = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if schema is None:
                continue
            if dst.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is None:
                dst.execute(schema[0])
            selector = "id" if table == "sessions" else "session_id"
            rows = src.execute(f"SELECT * FROM {table} WHERE {selector}=?", (session_id,))
            values = [
                tuple(clean_text(value, workspace) if isinstance(value, str) else value for value in row)
                for row in rows
            ]
            if values:
                dst.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in values[0])})", values
                )


def run(prompt: str, workspace: Path, session_id: str, *, resume: bool = False,
        attachment: Path | None = None) -> None:
    args = [
        "copilot", "-p", prompt, "--allow-all-tools", "--no-custom-instructions",
        "--disable-builtin-mcps", "--no-auto-update", "--no-remote-export",
        "--silent", "-C", str(workspace),
    ]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    if attachment:
        args += ["--attachment", str(attachment)]
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Copilot exited {result.returncode}: {result.stderr.strip()}")


def main() -> None:
    version = subprocess.run(
        ["copilot", "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    cases = {
        "baseline": "Reply with BASELINE_FIXTURE_MARKER and nothing else. Do not use tools.",
        "continued": "Remember the phrase BLUE-WREN-17, then reply with READY.",
        "tool_success": "Use the bash tool to run printf TOOL_FIXTURE_MARKER. Report its output.",
        "tool_failure": "Use the bash tool to run false. Report that it failed.",
        "skill": "Read and invoke the fixture-skill skill, then report its marker.",
        "image": "Describe the attached one-pixel image in one sentence.",
    }
    ROOT.mkdir(parents=True, exist_ok=True)
    # The CLI runs outside the repo; outputs stage beside ROOT so replace() stays on one filesystem.
    with tempfile.TemporaryDirectory(prefix="copilot-fixture-") as temporary, \
            tempfile.TemporaryDirectory(prefix="copilot-fixture-staging-", dir=ROOT.parent) as staging:
        work = Path(temporary)
        stage = Path(staging)
        db = stage / "session-store.db"
        manifest: dict[str, object] = {"cli_version": version, "cases": {}}
        for name, prompt in cases.items():
            workspace = work / name / "workspace"
            workspace.mkdir(parents=True)
            attachment = None
            if name == "skill":
                skill = workspace / ".github/skills/fixture-skill/SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(
                    "---\nname: fixture-skill\ndescription: A fixture capture skill.\n"
                    "---\n\nReply with SKILL_FIXTURE_MARKER.\n"
                )
            if name == "image":
                attachment = workspace / "pixel.png"
                attachment.write_bytes(PNG)
            session_id = str(uuid.uuid4())
            print(f"Capturing {name} ({session_id})", flush=True)
            run(prompt, workspace, session_id, attachment=attachment)
            if name == "continued":
                run("What phrase did I ask you to remember?", workspace, session_id,
                    resume=True)
            events = STATE / session_id / "events.jsonl"
            if not events.is_file():
                raise RuntimeError(f"{name}: missing native event log")
            lines = events.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines]
            event_types = [record.get("type") for record in records]
            if "user.message" not in event_types or "assistant.message" not in event_types:
                raise RuntimeError(f"{name}: incomplete native event log")
            if name == "continued" and event_types.count("user.message") < 2:
                raise RuntimeError("continued: resume did not produce a second turn")
            if name.startswith("tool_"):
                command = "printf TOOL_FIXTURE_MARKER" if name == "tool_success" else "false"
                starts = [record["data"] for record in records
                          if record.get("type") == "tool.execution_start"]
                completes = [record["data"] for record in records
                             if record.get("type") == "tool.execution_complete"]
                if not any(call.get("toolName") == "bash"
                           and call.get("arguments", {}).get("command") == command
                           and any(done.get("toolCallId") == call.get("toolCallId")
                                   for done in completes) for call in starts):
                    raise RuntimeError(f"{name}: missing native {command} execution")
            if name == "skill" and not any(
                record.get("type") == "skill.invoked"
                and record.get("data", {}).get("name") == "fixture-skill"
                for record in records
            ):
                raise RuntimeError("skill: no native skill invocation")
            if name == "image" and not any(
                record.get("type") == "user.message"
                and record.get("data", {}).get("attachments") for record in records
            ):
                raise RuntimeError("image: attachment was not recorded")
            expected = {"baseline": "BASELINE_FIXTURE_MARKER",
                        "continued": "BLUE-WREN-17"}.get(name)
            replies = [record.get("data", {}).get("content", "") for record in records
                       if record.get("type") == "assistant.message"]
            if expected and (not replies or expected not in replies[-1]):
                raise RuntimeError(f"{name}: missing expected reply {expected}")
            result = {"tool_success": "TOOL_FIXTURE_MARKER",
                      "tool_failure": "exit code 1"}.get(name)
            if result and not any(
                result in str(record.get("data", {}).get("result", ""))
                for record in records if record.get("type") == "tool.execution_complete"
            ):
                raise RuntimeError(f"{name}: missing native result {result}")
            copy_session(STORE, session_id, db, workspace)
            output = stage / name / "events.jsonl"
            output.parent.mkdir()
            output.write_text(clean_event_lines(lines, workspace), encoding="utf-8")
            manifest["cases"][name] = session_id

        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        target = ROOT
        for name in cases:
            dest = target / name
            dest.mkdir(exist_ok=True)
            (stage / name / "events.jsonl").replace(dest / "events.jsonl")
        for name in ("session-store.db", "manifest.json"):
            (stage / name).replace(target / name)
        (target / "pixel.png").write_bytes(PNG)
        (target / "fixture-skill.md").write_text(
            "---\nname: fixture-skill\ndescription: A fixture capture skill.\n"
            "---\n\nReply with SKILL_FIXTURE_MARKER.\n"
        )


if __name__ == "__main__":
    main()
