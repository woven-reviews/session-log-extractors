"""Copilot source readers and supplied-input interpretation."""

import base64
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import export_session

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"
CASES = json.loads((FIXTURES / "manifest.json").read_text())["cases"]
PNG = (FIXTURES / "pixel.png").read_bytes()


def _native_lines(case: str) -> list[str]:
    return (FIXTURES / case / "events.jsonl").read_text(encoding="utf-8").splitlines()


def _native_db():
    return sources.get_db_connection(FIXTURES / "session-store.db")


def _native_db_copy(tmp_path: Path, sql: str = "", params=()) -> Path:
    """Copy the native DB into tmp_path, optionally applying one edit."""
    db_path = tmp_path / "session-store.db"
    shutil.copyfile(FIXTURES / "session-store.db", db_path)
    if sql:
        with sqlite3.connect(db_path) as conn:
            conn.execute(sql, params)
    return db_path


def _install_native_events(case: str) -> None:
    _write_events_log(CASES[case], _native_lines(case))


@pytest.fixture(autouse=True)
def _isolate_copilot_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path / "session-state")


def _write_events_log(session_id: str, lines) -> Path:
    """Write raw JSONL lines to the isolated state root's events.jsonl."""
    session_dir = sources.COPILOT_STATE_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
    assert export_session(session) == expected
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


def test_copilot_prefix_matching():
    session_id = CASES["baseline"]
    with _native_db() as conn:
        assert sources.get_session_by_id(conn, session_id)["id"] == session_id
        assert sources.get_session_by_id(conn, session_id[:8])["id"] == session_id


def test_copilot_get_db_connection_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        sources.get_db_connection(Path("/nonexistent/copilot/session-store.db"))


def test_copilot_table_columns_without_row_factory():
    conn = sqlite3.connect(":memory:")  # no row_factory set -> plain tuples
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    assert sources._table_columns(conn, "t") == {"id", "name"}


def test_copilot_paths_overlap_both_directions_and_neither(tmp_path):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    other = tmp_path / "c"
    other.mkdir()
    assert sources._paths_overlap(tmp_path, child) is True
    assert sources._paths_overlap(child, tmp_path) is True
    assert sources._paths_overlap(child, other) is False


def test_copilot_cwd_matches_blank_and_strict(tmp_path):
    assert sources._cwd_matches(None, Path.cwd()) is False
    assert sources._cwd_matches("", Path.cwd()) is False
    assert sources._cwd_matches(str(Path.cwd()), Path.cwd(), strict=True) is True
    assert sources._cwd_matches(str(tmp_path / "sub"), tmp_path, strict=True) is False


def test_copilot_newest_session_falls_back_to_global_newest():
    # Native sessions ran in /workspace, which never overlaps the test cwd.
    with _native_db() as conn:
        session = sources.newest_session(conn, Path.cwd(), strict=False)
    assert session["id"] == CASES["image"]


def test_copilot_newest_session_strict_with_no_match_returns_none():
    with _native_db() as conn:
        assert sources.newest_session(conn, Path.cwd(), strict=True) is None


def test_copilot_newest_session_returns_none_for_empty_db(tmp_path):
    db_path = _native_db_copy(tmp_path, "DELETE FROM sessions")
    with sources.get_db_connection(db_path) as conn:
        assert sources.newest_session(conn, Path.cwd(), strict=False) is None


def test_copilot_newest_session_picks_most_recently_updated():
    # The image case was captured last, so it has the latest updated_at.
    with _native_db() as conn:
        session = sources.newest_session(conn, Path("/workspace"), strict=True)
    assert session["id"] == CASES["image"]


def test_copilot_get_session_models_reads_native_usage_events():
    # The continued session logs one usage event per turn, both on one model.
    with _native_db() as conn:
        assert sources.get_session_models(conn, CASES["continued"]) == [
            "claude-sonnet-5"
        ]


def test_copilot_get_session_models_falls_back_to_legacy_events_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events (session_id TEXT, usage_model TEXT, timestamp TEXT)"
    )
    conn.executemany(
        "INSERT INTO events VALUES ('s', ?, ?)",
        [
            ("gpt-4", "2026-01-01"),
            ("", "2026-01-02"),
            ("gpt-4", "2026-01-03"),
            ("o3", "2026-01-04"),
        ],
    )
    assert sources.get_session_models(conn, "s") == ["gpt-4", "o3"]


def test_copilot_get_session_models_without_usage_table_is_empty():
    assert sources.get_session_models(sqlite3.connect(":memory:"), "any") == []


def test_copilot_reader_missing_image_returns_none():
    assert sources.read_image("/does/not/exist.png") is None


def test_copilot_retained_database_metadata_readers(tmp_path):
    session_id = CASES["baseline"]
    db_path = _native_db_copy(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_refs (session_id, ref_type, ref_value, turn_index, created_at) VALUES (?, 'commit', 'abc123', 0, '2026-01-01T00:00:00Z')",
            (session_id,),
        )
        conn.execute(
            "INSERT INTO checkpoints (session_id, checkpoint_number, title, overview, created_at) VALUES (?, 1, 'Checkpoint one', 'did stuff', '2026-01-01T00:05:00Z')",
            (session_id,),
        )
    with sources.get_db_connection(db_path) as conn:
        assert sources.get_session_refs(conn, session_id) == [
            {
                "ref_type": "commit",
                "ref_value": "abc123",
                "turn_index": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        assert sources.get_session_checkpoints(conn, session_id) == [
            {
                "checkpoint_number": 1,
                "title": "Checkpoint one",
                "overview": "did stuff",
                "created_at": "2026-01-01T00:05:00Z",
            }
        ]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("with_state", [False, True])
def test_copilot_loaded_inputs_interpret_without_io(case, with_state, monkeypatch):
    if with_state:
        _install_native_events(case)
    conn = _native_db()
    try:
        session = sources.get_session_by_id(conn, CASES[case])
        expected = copilot.extract_session(conn, session)
        loaded = copilot.read_source(conn, session)
    finally:
        conn.close()

    def forbidden(*args, **kwargs):
        pytest.fail("Interpretation attempted source access")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    assert loaded.session == session
    assert copilot.build_raw_envelope(loaded) == expected
    assert copilot.build_raw_envelope(loaded) == expected


def test_copilot_extraction_reads_and_parses_state_once(monkeypatch):
    _install_native_events("image")
    # Count reads on the concrete state path, leaving fixture setup outside.
    original_read = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)

    loads = Mock(wraps=json.loads)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(json, "loads", loads)
    with _native_db() as conn:
        session = sources.get_session_by_id(conn, CASES["image"])
        copilot.extract_session(conn, session)
    assert len(reads) == 1
    assert loads.call_count == len(original_read(reads[0]).splitlines())


@pytest.mark.parametrize("inline", ["data_url", "data", "invalid", "path"])
def test_copilot_loaded_path_image_precedence(tmp_path, monkeypatch, inline):
    session_id = CASES["image"]
    events = [json.loads(line) for line in _native_lines("image")]
    # Replace the native asset reference with each legacy encoding. The path
    # exists at loading time and is removed before interpretation.
    path = tmp_path / "pixel.png"
    path.write_bytes(PNG)
    for event in events:
        if event["type"] == "user.message":
            attachment = event["data"]["attachments"][0]
            attachment.pop("assetId", None)
            attachment["path"] = str(path)
            if inline == "data_url":
                attachment["dataUrl"] = (
                    "data:image/png;base64," + base64.b64encode(b"inline").decode()
                )
            elif inline in ("data", "invalid"):
                attachment["data"] = (
                    "A" if inline == "invalid" else base64.b64encode(b"inline").decode()
                )
                attachment["mediaType"] = "image/png"
    _write_events_log(session_id, [json.dumps(event) for event in events])
    with _native_db() as conn:
        loaded = copilot.read_source(conn, sources.get_session_by_id(conn, session_id))
    path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Interpretation read an image file")

    monkeypatch.setattr(Path, "open", forbidden)
    envelope = copilot.build_raw_envelope(loaded)
    image = next(e for e in envelope["events"] if e["role"] == "user")["images"][0]
    if inline == "invalid":
        assert image["unavailable"] is True
    else:
        assert base64.b64decode(image["data"]) == (
            PNG if inline == "path" else b"inline"
        )


def test_copilot_preloaded_image_extension_is_per_reference():
    refs = [{"path": "/noext", "type": mime} for mime in ("image/png", "image/jpeg")]
    images = copilot.envelope_images(refs, {"/noext": PNG})
    assert [image["media_type"] for image in images] == ["image/png", "image/jpeg"]
