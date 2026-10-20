"""Turn a fetched ``session_logs/`` tree into the envelopes the existing extractors produce.

The server fetches the ``session-logs`` branch and runs this over it. Claude and Codex
transcripts go straight to their extractor with ``--transcript``. Copilot needs its native
layout, so the captured ``events.jsonl`` and ``copilot_db.json`` are rebuilt into a throwaway
``~/.copilot`` and the Copilot extractor runs with ``HOME`` pointed at it.

Run it as ``python3 -m session_log_extractors.capture.export SESSION_LOGS_DIR OUT_DIR``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .records import COPILOT_DB_COLUMNS


@dataclass
class ExportResult:
    files: list[Path] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def rebuild_copilot_home(session_dir: Path, home: Path) -> Path:
    """Recreate ``<home>/.copilot`` for one captured Copilot session; returns the db path."""
    session_id = session_dir.name
    state = home / ".copilot" / "session-state" / session_id
    state.mkdir(parents=True)
    (state / "events.jsonl").write_bytes((session_dir / "events.jsonl").read_bytes())
    db_path = home / ".copilot" / "session-store.db"
    dump_file = session_dir / "copilot_db.json"
    dump = (
        json.loads(dump_file.read_text(encoding="utf-8")) if dump_file.exists() else {}
    )
    conn = sqlite3.connect(db_path)
    try:
        for table, columns in COPILOT_DB_COLUMNS.items():
            conn.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
            for row in dump.get(table, []):
                names = [c for c in columns if c in row]
                marks = ", ".join("?" for _ in names)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})",
                    [row[n] for n in names],
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _run(
    argv: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def _collect(
    result: ExportResult,
    key: str,
    proc: subprocess.CompletedProcess[str],
    found: list[Path],
) -> None:
    if proc.returncode != 0 or not found:
        result.failures[key] = (proc.stderr or proc.stdout)[
            -400:
        ] or "no envelope produced"
    result.files.extend(found)


def _export_claude(
    transcript: Path, out_dir: Path, scripts: Path, result: ExportResult
) -> None:
    target = out_dir / f"claude_session_log_raw_{transcript.stem}.json"
    proc = _run(
        [
            str(scripts / "extract_claude_session_log.py"),
            "--transcript",
            str(transcript),
            "--output",
            str(target),
        ]
    )
    _collect(
        result, f"claude/{transcript.stem}", proc, [target] if target.exists() else []
    )


def _export_codex(
    transcript: Path, out_dir: Path, scripts: Path, result: ExportResult
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(
            [
                str(scripts / "extract_codex_session_log.py"),
                "--transcript",
                str(transcript),
                "--output",
                f"{tmp}/log.md",
            ]
        )
        found = []
        for produced in sorted(Path(tmp).glob("codex_session_log_raw_*.json")):
            target = out_dir / produced.name
            target.write_bytes(produced.read_bytes())
            found.append(target)
    _collect(result, f"codex/{transcript.stem}", proc, found)


def _export_copilot(
    session_dir: Path, out_dir: Path, scripts: Path, result: ExportResult
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        db_path = rebuild_copilot_home(session_dir, home)
        argv = [
            str(scripts / "extract_copilot_session_log.py"),
            session_dir.name,
            "--db",
            str(db_path),
        ]
        proc = _run(
            [*argv, "--output", f"{tmp}/log.md"],
            {"HOME": str(home), "COPILOT_HOME": str(home / ".copilot")},
        )
        found = []
        for produced in sorted(Path(tmp).glob("copilot_session_log_raw_*.json")):
            target = out_dir / produced.name
            target.write_bytes(produced.read_bytes())
            found.append(target)
    _collect(result, f"copilot/{session_dir.name}", proc, found)


def export_sessions(session_logs: Path, out_dir: Path, scripts: Path) -> ExportResult:
    """Run the matching extractor over every captured session; failures are reported, not raised."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult()
    for transcript in sorted((session_logs / "claude").glob("*.jsonl")):
        _export_claude(transcript, out_dir, scripts, result)
    for transcript in sorted((session_logs / "codex").glob("*.jsonl")):
        _export_codex(transcript, out_dir, scripts, result)
    for session_dir in sorted(
        p for p in (session_logs / "copilot").glob("*") if (p / "events.jsonl").exists()
    ):
        _export_copilot(session_dir, out_dir, scripts, result)
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: export SESSION_LOGS_DIR OUT_DIR", file=sys.stderr)
        return 2
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    result = export_sessions(Path(argv[0]), Path(argv[1]), scripts)
    print(
        json.dumps(
            {"files": [str(f) for f in result.files], "failures": result.failures}
        )
    )
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
