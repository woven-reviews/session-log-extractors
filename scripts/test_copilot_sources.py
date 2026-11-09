"""Copilot source readers and supplied-input interpretation."""

import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from copilot_fixture import (
    ASSETS,
    DATABASE,
    SESSION_ID,
    install_native_events,
    native_db_copy,
    native_lines,
    native_source,
    single_image_source,
    write_events_log,
)

from session_log_extractors import copilot_protocol
from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import export_session

PNG = (ASSETS / "red.png").read_bytes()


def _native_db():
    return sources.get_db_connection(DATABASE)


@pytest.fixture(autouse=True)
def _isolate_copilot_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path / "session-state")


def test_supplied_source_interprets_to_shared_session_without_io(monkeypatch):
    conn = sources.get_db_connection(DATABASE)
    try:
        metadata = sources.get_session_by_id(conn, SESSION_ID)
        turns = sources.get_session_turns(conn, metadata["id"])
        models = sources.get_session_models(conn, metadata["id"])
    finally:
        conn.close()
    records = native_source().state_records
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
    exported = export_session(session)
    assert exported == expected
    actual_envelope = copilot.build_raw_envelope(source)
    assert actual_envelope == expected
    assert source.image_inputs == {}


def test_reader_snapshots_optional_assets_once(tmp_path, monkeypatch):
    image = tmp_path / "pixel.png"
    image.write_bytes((ASSETS / "red.png").read_bytes())
    missing = tmp_path / "missing.png"
    image_references = []
    for path in (image, missing, image):
        image_references.append({"path": str(path)})
    image_references += [{"path": 123}, {"path": " "}, {}]
    original = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    actual_images = sources.load_image_inputs(image_references)
    assert actual_images == {
        str(image): original(image),
        str(missing): None,
    }
    assert reads == [image, missing]

    def unreadable(*args, **kwargs):
        raise PermissionError("asset denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    actual_images = sources.load_image_inputs(image_references)
    assert actual_images == {str(image): None, str(missing): None}


def test_source_reader_keeps_connection_owned_by_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path)
    conn = sources.get_db_connection(DATABASE)
    try:
        metadata = sources.get_session_by_id(conn, SESSION_ID)
        source = sources.read_source(conn, metadata)
        assert source.session == metadata
        assert source.session is not metadata
        expected_turns = sources.get_session_turns(conn, metadata["id"])
        assert source.turns == expected_turns
        assert source.state_records == []
        # Loading neither closes the caller's connection nor changes read-only mode.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sessions")
    finally:
        conn.close()


def test_copilot_prefix_matching():
    session_id = SESSION_ID
    with _native_db() as conn:
        actual_get_session_by_id = sources.get_session_by_id(conn, session_id)
        assert actual_get_session_by_id["id"] == session_id
        actual_get_session_by_id = sources.get_session_by_id(conn, session_id[:8])
        assert actual_get_session_by_id["id"] == session_id


def test_copilot_get_db_connection_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        sources.get_db_connection(Path("/nonexistent/copilot/session-store.db"))


def test_copilot_table_columns_without_row_factory():
    conn = sqlite3.connect(":memory:")  # no row_factory set -> plain tuples
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    actual_table_columns = sources._table_columns(conn, "t")
    assert actual_table_columns == {"id", "name"}


def test_copilot_newest_session_falls_back_to_global_newest():
    # Native sessions ran in /workspace, which never overlaps the test cwd.
    with _native_db() as conn:
        session = sources.newest_session(conn, Path.cwd(), strict=False)
    assert session["id"] == SESSION_ID


def test_copilot_newest_session_strict_with_no_match_returns_none():
    with _native_db() as conn:
        actual_newest_session = sources.newest_session(conn, Path.cwd(), strict=True)
        assert actual_newest_session is None


def test_copilot_newest_session_returns_none_for_empty_db(tmp_path):
    db_path = native_db_copy(tmp_path, "DELETE FROM sessions")
    with sources.get_db_connection(db_path) as conn:
        actual_newest_session = sources.newest_session(conn, Path.cwd(), strict=False)
        assert actual_newest_session is None


def test_copilot_newest_session_picks_most_recently_updated(tmp_path):
    # A second controlled identity makes recency selection observable.
    db_path = native_db_copy(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id,cwd,created_at,updated_at) SELECT 'older',cwd,created_at,'2000-01-01' FROM sessions WHERE id=?",
            (SESSION_ID,),
        )
    with sources.get_db_connection(db_path) as conn:
        session = sources.newest_session(conn, Path("/workspace"), strict=True)
    assert session["id"] == SESSION_ID


def test_copilot_get_session_models_reads_native_usage_events():
    expected = native_source().models
    with _native_db() as conn:
        actual = sources.get_session_models(conn, SESSION_ID)
    assert actual == expected
    assert len(actual) >= 2
    assert len(actual) == len(set(actual))


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
    actual_get_session_models = sources.get_session_models(conn, "s")
    assert actual_get_session_models == ["gpt-4", "o3"]


def test_copilot_get_session_models_without_usage_table_is_empty():
    actual_get_session_models = sources.get_session_models(
        sqlite3.connect(":memory:"), "any"
    )
    assert actual_get_session_models == []


def test_copilot_reader_missing_image_returns_none():
    actual_read_image = sources.read_image("/does/not/exist.png")
    assert actual_read_image is None


def test_copilot_retained_database_metadata_readers(tmp_path):
    session_id = SESSION_ID
    db_path = native_db_copy(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_refs (session_id, ref_type, ref_value, turn_index, created_at) VALUES (?, 'commit', 'abc123', 0, '2026-01-01T00:00:00Z')",
            (session_id,),
        )
    with sources.get_db_connection(db_path) as conn:
        actual_get_session_refs = sources.get_session_refs(conn, session_id)
        assert actual_get_session_refs == [
            {
                "ref_type": "commit",
                "ref_value": "abc123",
                "turn_index": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        actual_checkpoints = sources.get_session_checkpoints(conn, session_id)
    captured = json.loads((DATABASE.parent / "session-store.json").read_text())
    expected_checkpoints = []
    for row in captured["checkpoints"]:
        expected = {}
        for field in ["checkpoint_number", "title", "overview", "created_at"]:
            expected[field] = row[field]
        expected_checkpoints.append(expected)
    assert actual_checkpoints == expected_checkpoints
    assert actual_checkpoints, "Required compaction checkpoint is missing"


@pytest.mark.parametrize("with_state", [False, True])
def test_copilot_loaded_inputs_interpret_without_io(with_state, monkeypatch):
    if with_state:
        install_native_events(sources.COPILOT_STATE_ROOT)
    conn = _native_db()
    try:
        session = sources.get_session_by_id(conn, SESSION_ID)
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
    actual_envelope = copilot.build_raw_envelope(loaded)
    assert actual_envelope == expected
    actual_envelope = copilot.build_raw_envelope(loaded)
    assert actual_envelope == expected


def test_copilot_extraction_reads_and_parses_state_once(monkeypatch):
    install_native_events(sources.COPILOT_STATE_ROOT)
    # Count reads on the concrete state path, leaving fixture setup outside.
    original_open = Path.open
    reads = []

    def read(path, *args, **kwargs):
        if path.name == "events.jsonl":
            reads.append(path)
        return original_open(path, *args, **kwargs)

    loads = Mock(wraps=json.loads)
    monkeypatch.setattr(Path, "open", read)
    monkeypatch.setattr(json, "loads", loads)
    with _native_db() as conn:
        session = sources.get_session_by_id(conn, SESSION_ID)
        copilot.extract_session(conn, session)
    assert len(reads) == 1
    assert loads.call_count == len(native_lines())


@pytest.mark.parametrize("inline", ["data_url", "data", "invalid", "path"])
def test_copilot_loaded_path_image_precedence(tmp_path, monkeypatch, inline):
    session_id = SESSION_ID
    events = single_image_source().state_records
    # Replace the native asset reference with each legacy encoding. The path
    # exists at loading time and is removed before interpretation.
    path = tmp_path / "pixel.png"
    path.write_bytes(PNG)
    for event in events:
        if event["type"] == copilot_protocol.USER_MESSAGE:
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
    event_lines = []
    for event in events:
        event_lines.append(json.dumps(event))
    write_events_log(sources.COPILOT_STATE_ROOT, session_id, event_lines)
    with _native_db() as conn:
        loaded = copilot.read_source(conn, sources.get_session_by_id(conn, session_id))
    path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Interpretation read an image file")

    monkeypatch.setattr(Path, "open", forbidden)
    envelope = copilot.build_raw_envelope(loaded)
    image_messages = []
    for event in envelope["events"]:
        if event.get("images"):
            image_messages.append(event)
    image = next(iter(image_messages))["images"][0]
    if inline == "invalid":
        assert image["unavailable"] is True
    else:
        assert base64.b64decode(image["data"]) == (
            PNG if inline == "path" else b"inline"
        )


def test_copilot_preloaded_image_extension_is_per_reference():
    image_references = []
    for mime in ("image/png", "image/jpeg"):
        image_references.append({"path": "/noext", "type": mime})
    images = copilot.envelope_images(image_references, {"/noext": PNG})
    actual_media_types = []
    for image in images:
        actual_media_types.append(image["media_type"])
    assert actual_media_types == ["image/png", "image/jpeg"]


def test_empty_loaded_source_interprets_without_io(monkeypatch):
    captured = native_source()
    # Remove conversation evidence, retaining native metadata and model history.
    source = sources.CopilotSource(captured.session, [], models=captured.models)

    def forbidden(*args, **kwargs):
        pytest.fail("Empty interpretation attempted source access")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    session = copilot.EXTRACTOR.interpret_source(source)
    envelope = copilot.build_raw_envelope(source)
    assert session["events"] == []
    assert session["models"] == captured.models
    assert session["cwd"] == captured.session["cwd"]
    assert session["started_at"] == captured.session["created_at"]
    assert envelope["events"] == []
    assert envelope["models"] == captured.models
