"""A captured session must read the same through the export path as the native capture does.

Each fixture is extracted twice: straight from the native capture, and after a pass-through
filter plus the export path the server uses. The envelopes' conversation events must match.
The real filter is a separate plug-in; this checks only the capture and export plumbing.
"""

import json
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from session_log_extractors.capture import export, locate

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = SCRIPTS / "fixtures"


@pytest.fixture
def identity_filter(tmp_path):
    from conftest import write_filter

    from session_log_extractors.capture import session_filter

    return session_filter.load(write_filter(tmp_path / "plugin", "identity"))


def events_of(envelope_path):
    events = json.loads(Path(envelope_path).read_text())["events"]
    return [
        (
            e.get("role"),
            e.get("text"),
            [
                (c.get("name"), json.dumps(c.get("input"), sort_keys=True))
                for c in e.get("tool_calls") or []
            ],
        )
        for e in events
    ]


def run(script, *args, env=None):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


@pytest.mark.parametrize(
    "fixture", sorted((FIXTURES / "claude").glob("*.jsonl")), ids=lambda p: p.stem
)
def test_claude_events_survive_capture_and_export(fixture, tmp_path, identity_filter):
    run(
        "extract_claude_session_log.py",
        "--transcript",
        str(fixture),
        "--output",
        str(tmp_path / "raw.json"),
    )
    tree = tmp_path / "tree"
    (tree / "claude").mkdir(parents=True)
    (tree / "claude" / "sid.jsonl").write_text(
        identity_filter.apply("filter_claude", fixture.read_text())
    )

    result = export.export_sessions(tree, tmp_path / "out", SCRIPTS)

    assert result.failures == {}
    assert events_of(result.files[0]) == events_of(tmp_path / "raw.json")


@pytest.mark.parametrize(
    "fixture", sorted((FIXTURES / "codex").glob("*.jsonl")), ids=lambda p: p.stem
)
def test_codex_events_survive_capture_and_export(fixture, tmp_path, identity_filter):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    run(
        "extract_codex_session_log.py",
        "--transcript",
        str(fixture),
        "--output",
        str(raw_dir / "native.json"),
    )
    tree = tmp_path / "tree"
    (tree / "codex").mkdir(parents=True)
    (tree / "codex" / "sid.jsonl").write_text(
        identity_filter.apply("filter_codex", fixture.read_text())
    )

    result = export.export_sessions(tree, tmp_path / "out", SCRIPTS)

    assert result.failures == {}
    assert events_of(result.files[0]) == events_of(raw_dir / "native.json")


def copilot_cases():
    manifest = json.loads((FIXTURES / "copilot" / "manifest.json").read_text())
    return sorted(manifest["cases"].items())


@pytest.mark.parametrize(("case", "session_id"), copilot_cases(), ids=lambda v: str(v))
def test_copilot_events_survive_capture_and_export(
    case, session_id, tmp_path, identity_filter
):
    events = (FIXTURES / "copilot" / case / "events.jsonl").read_text()
    native = tmp_path / "native" / ".copilot"
    (native / "session-state" / session_id).mkdir(parents=True)
    (native / "session-state" / session_id / "events.jsonl").write_text(events)
    shutil.copy(FIXTURES / "copilot" / "session-store.db", native / "session-store.db")
    env = {"HOME": str(native.parent), "PATH": "/usr/bin:/bin"}
    run(
        "extract_copilot_session_log.py",
        session_id,
        "--db",
        str(native / "session-store.db"),
        "--output",
        str(tmp_path / "raw.json"),
        env=env,
    )
    dump = locate.copilot_db_dump(native / "session-store.db", session_id)
    session_dir = tmp_path / "tree" / "copilot" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "events.jsonl").write_text(
        identity_filter.apply("filter_copilot_events", events)
    )
    (session_dir / "copilot_db.json").write_text(
        identity_filter.apply("filter_copilot_db", json.dumps(dump))
    )

    result = export.export_sessions(tmp_path / "tree", tmp_path / "out", SCRIPTS)

    assert result.failures == {}
    assert events_of(result.files[0]) == events_of(tmp_path / "raw.json")


def test_export_reports_failures_instead_of_raising(tmp_path):
    tree = tmp_path / "tree"
    (tree / "claude").mkdir(parents=True)
    (tree / "claude" / "empty.jsonl").write_text("")
    (tree / "codex").mkdir()
    (tree / "codex" / "empty.jsonl").write_text("")
    (tree / "copilot" / "c1").mkdir(parents=True)
    (tree / "copilot" / "c1" / "events.jsonl").write_text("")

    result = export.export_sessions(tree, tmp_path / "out", SCRIPTS)

    assert set(result.failures) == {"claude/empty", "codex/empty", "copilot/c1"}


def test_rebuild_copilot_home_without_a_dump_still_creates_the_schema(tmp_path):
    session = tmp_path / "copilot" / "c9"
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("{}\n")

    db_path = export.rebuild_copilot_home(session, tmp_path / "home")

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
    assert (
        tmp_path / "home" / ".copilot" / "session-state" / "c9" / "events.jsonl"
    ).exists()


def test_export_main_prints_a_summary_and_validates_usage(tmp_path, capsys):
    assert export.main([]) == 2
    assert "usage" in capsys.readouterr().err
    (tmp_path / "tree").mkdir()

    code = export.main([str(tmp_path / "tree"), str(tmp_path / "out")])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"files": [], "failures": {}}


def test_export_module_runs_as_a_script(tmp_path, monkeypatch, capsys):
    (tmp_path / "tree").mkdir()
    argv = ["export", str(tmp_path / "tree"), str(tmp_path / "out")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delitem(sys.modules, "session_log_extractors.capture.export")

    with pytest.raises(SystemExit) as info:
        runpy.run_module("session_log_extractors.capture.export", run_name="__main__")

    assert info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"files": [], "failures": {}}
