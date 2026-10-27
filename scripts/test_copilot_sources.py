"""Copilot source readers and supplied-input interpretation."""

import json
import sqlite3
from pathlib import Path

import pytest

from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import build_envelope

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"
CASES = json.loads((FIXTURES / "manifest.json").read_text())["cases"]


def test_supplied_source_interprets_to_shared_session_without_io(monkeypatch):
    conn = sources.get_db_connection(FIXTURES / "session-store.db")
    try:
        metadata = sources.get_session_by_id(conn, CASES["image"])
        turns = sources.get_session_turns(conn, metadata["id"])
        models = sources.get_session_models(conn, metadata["id"])
    finally:
        conn.close()
    records = sources.parse_state_records(
        (FIXTURES / "image" / "events.jsonl").read_text().splitlines()
    )
    source = sources.CopilotSource(
        metadata, turns, state_records=records, models=models
    )
    expected = copilot.build_raw_envelope(source)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed a source")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    for method in ("open", "exists", "is_file", "resolve", "expanduser"):
        monkeypatch.setattr(Path, method, forbidden)
    session = copilot.interpret_source(source)
    assert session["harness"] == "copilot"
    assert session["started_at"] == metadata["created_at"]
    assert session["models"] == models
    assert build_envelope(session) == expected
    assert copilot.build_raw_envelope(source) == expected
    assert source.image_inputs == {}


def test_reader_snapshots_optional_assets_once(tmp_path, monkeypatch):
    image = tmp_path / "pixel.png"
    image.write_bytes((FIXTURES / "pixel.png").read_bytes())
    missing = tmp_path / "missing.png"
    refs = [{"path": str(path)} for path in (image, missing, image)]
    refs += [{"path": 123}, {"path": " "}, {}]
    original = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert sources.load_image_inputs(refs) == {
        str(image): original(image),
        str(missing): None,
    }
    assert reads == [image, missing]

    def unreadable(*args, **kwargs):
        raise PermissionError("asset denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert sources.load_image_inputs(refs) == {str(image): None, str(missing): None}


def test_source_reader_keeps_connection_owned_by_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path)
    conn = sources.get_db_connection(FIXTURES / "session-store.db")
    try:
        metadata = sources.get_session_by_id(conn, CASES["baseline"])
        source = sources.read_source(conn, metadata)
        assert source.session == metadata
        assert source.session is not metadata
        assert source.turns == sources.get_session_turns(conn, metadata["id"])
        assert source.state_records == []
        # Loading neither closes the caller's connection nor changes read-only mode.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sessions")
    finally:
        conn.close()
