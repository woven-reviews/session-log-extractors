"""Tests for extract_copilot_session_log.

Coverage: pytest --cov=scripts --cov-report=term-missing \
    scripts/test_extract_copilot_session_log.py
"""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from copilot_fixture import (
    ASSETS,
    DATABASE,
    ROOT,
    SESSION_ID,
    TRANSCRIPT,
    native_prose_source,
    native_source,
    native_tool_source,
    recorded_calls,
    single_image_source,
)

from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot_log
from session_log_extractors.copilot_sources import (
    get_db_connection,
    get_session_by_id,
    get_session_turns,
    matching_sessions,
)
from session_log_extractors.extract_copilot_session_log import (
    _permissions_from_events,
    _skills_from_events,
)

FIXTURES = ROOT
PNG = (ASSETS / "red.png").read_bytes()


def _native_lines() -> list[str]:
    return TRANSCRIPT.read_text(encoding="utf-8").splitlines()


def _call_names(session):
    names = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            names.append(call["name"])
    return names


def _native_db():
    return get_db_connection(DATABASE)


def _native_db_copy(tmp_path: Path, sql: str = "", params=()) -> Path:
    """Copy the native DB into tmp_path, optionally applying one edit."""
    db_path = tmp_path / "session-store.db"
    shutil.copyfile(DATABASE, db_path)
    if sql:
        with sqlite3.connect(db_path) as conn:
            conn.execute(sql, params)
    return db_path


def _install_native_events() -> None:
    _write_events_log(SESSION_ID, _native_lines())


def _hand_lines(name: str) -> list[str]:
    return (FIXTURES / "boundary" / name).read_text(encoding="utf-8").splitlines()


@pytest.fixture(autouse=True)
def _isolate_copilot_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path / "session-state")


def _make_test_db(tmp_path, session_id, turns):
    """Copy the native DB schema with one session holding the given turns."""
    db_path = _native_db_copy(tmp_path)
    with sqlite3.connect(db_path) as conn:
        for table in (
            "sessions",
            "turns",
            "checkpoints",
            "session_files",
            "session_refs",
            "assistant_usage_events",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                str(Path.cwd()),
                "test/repo",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:00:00Z",
            ),
        )
        for turn in turns:
            conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, "
                "assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    turn["turn_index"],
                    turn["user_message"],
                    turn["assistant_response"],
                    turn["timestamp"],
                ),
            )
    return db_path


def _write_events_log(session_id: str, lines) -> Path:
    """Write raw JSONL lines to the isolated state root's events.jsonl."""
    session_dir = sources.COPILOT_STATE_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_copilot_permissions_join_approval_denial_and_pending():
    perms = _permissions_from_events(
        sources.parse_state_records(_hand_lines("permissions.jsonl"))
    )
    actual_permissions = []
    for p in perms:
        actual_permissions.append(
            (p["tool"], p["native_decision"], p["feedback"], p["ts"])
        )
    assert actual_permissions == [
        ("pytest", "approved", "", "2026-07-01T22:04:04Z"),
        (
            "edit crud.py",
            "denied-interactively-by-user",
            "not like that",
            "2026-07-01T22:05:30Z",
        ),
        ("rm x", None, "", "2026-07-01T22:06:00Z"),
    ]


def test_copilot_db_operations():
    _install_native_events()
    with _native_db() as conn:
        session = get_session_by_id(conn, SESSION_ID)
        turns = get_session_turns(conn, session["id"])
        matches = matching_sessions(conn, Path("/workspace"), strict=True)
        envelope = copilot_log.extract_session(conn, session)
    assert session["cwd"] == "/workspace"
    assert len(turns) > 1
    assert matches == [session]
    assert envelope["cwd"] == "/workspace"
    assert envelope["started_at"] == session["created_at"]
    assert envelope["models"] == native_source().models
    assert len(envelope["models"]) >= 2
    for turn in turns:
        assert turn["assistant_response"] is None or isinstance(
            turn["assistant_response"], str
        )


def test_copilot_native_fixture_ids_match_db_and_events():
    with _native_db() as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == SESSION_ID
    records = sources.parse_state_records(_native_lines())
    starts = []
    resumes = []
    for record in records:
        if record["type"] == "session.start":
            starts.append(record)
        elif record["type"] == "session.resume":
            resumes.append(record)
    assert len(starts) == len(resumes) == 1
    assert starts[0]["data"]["sessionId"] == SESSION_ID


def test_copilot_image_reference_without_attachment_is_flagged(tmp_path):
    db_path = _make_test_db(
        tmp_path,
        "img-session",
        [
            {
                "turn_index": 0,
                "user_message": "[image: pixel.png] please summarize",
                "assistant_response": "Done",
                "timestamp": "2026-01-01T12:00:05Z",
            }
        ],
    )
    with get_db_connection(db_path) as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, "img-session")
        )
    assert envelope["events"][0]["images"] == [
        {"unavailable": True, "ref": "pixel.png"}
    ]
    assert envelope["events"][0]["text"] == "[image: pixel.png] please summarize"


def test_copilot_image_attachment_is_inlined_from_db(tmp_path):
    img_path = tmp_path / "pixel.png"
    shutil.copyfile(ASSETS / "red.png", img_path)
    db_path = _make_test_db(
        tmp_path,
        session_id="img-session-2",
        turns=[
            {
                "turn_index": 0,
                "user_message": "[image: pixel.png] please summarize",
                "assistant_response": "Done",
                "timestamp": "2026-01-01T12:00:05Z",
            }
        ],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE attachments (session_id TEXT, display_name TEXT, path TEXT, type TEXT)"
        )
        conn.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?)",
            ("img-session-2", "pixel.png", str(img_path), "image/png"),
        )
    with get_db_connection(db_path) as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, "img-session-2")
        )
    assert base64.b64decode(envelope["events"][0]["images"][0]["data"]) == PNG
    assert not (tmp_path / "images").exists()


def test_copilot_native_image_attachment_is_inlined():
    assert not Path("/workspace/red.png").exists()
    _install_native_events()
    with _native_db() as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, SESSION_ID)
        )
    users = []
    for event in envelope["events"]:
        if event.get("images"):
            users.append(event)
    assert len(users) == 2
    assert len(users[0]["images"]) == 2
    assert len(users[1]["images"]) == 3
    assert base64.b64decode(users[0]["images"][0]["data"]) == PNG


def test_copilot_attachment_without_its_binary_asset_keeps_the_path():
    records = single_image_source().state_records
    lines = []
    for record in records:
        if record["type"] != "session.binary_asset":
            lines.append(json.dumps(record))
    _write_events_log("sess-asset", lines)
    [record] = copilot_log.get_state_attachments("sess-asset")
    assert record["path"] == "/workspace/red.png"
    assert "data" not in record


def test_copilot_native_tool_success_keeps_full_call():
    source = native_tool_source()
    session = copilot_log.interpret_source(source)
    calls = list(recorded_calls(session).values())
    assert len(calls) == 1
    (call,) = calls
    assert call["name"] == "bash"
    assert "CAPTURE_STDOUT_OK" in call["input"]["command"]
    assert "CAPTURE_STDOUT_OK" in call["result"]
    assert "CAPTURE_STDERR_OK" in call["result"]


def test_copilot_native_tool_failure_keeps_shell_exit_code():
    session = copilot_log.interpret_source(native_source())
    failures = []
    for call in recorded_calls(session).values():
        if call["name"] == "bash" and "CAPTURE_ERROR_7" in call["input"].get(
            "command", ""
        ):
            failures.append(call)
    assert len(failures) == 1
    (call,) = failures
    assert "exit code 7" in call["result"]
    assert call["is_error"] is True


def test_copilot_main_defaults_to_all_without_a_selector(monkeypatch, tmp_path):
    called = {}

    def fake_extract_all(conn, output, strict, tool_result_max_bytes, *, project_root):
        called["output"] = output
        called["strict"] = strict
        called["project_root"] = project_root
        return 0

    monkeypatch.setattr(copilot_log, "_extract_all", fake_extract_all)
    monkeypatch.setattr(
        sources, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    rc = copilot_log.main(["--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert called == {"output": None, "strict": False, "project_root": Path.cwd()}


# -- tool/result/skill descriptor edge cases -------------------------------


def test_decode_arguments_parses_json_string_and_falls_back_on_invalid():
    assert copilot_log._decode_arguments('{"a": 1}') == {"a": 1}
    assert copilot_log._decode_arguments("not json") == "not json"
    assert copilot_log._decode_arguments({"a": 1}) == {"a": 1}


def test_skill_refs_from_call_finds_named_skill_and_fills_in_its_path():
    # The "skill" call registers the name with no path; the SKILL.md regex
    # match for the same name later fills it in.
    refs = copilot_log.skill_refs_from_call(
        "builtin.skill",
        {"skill": "demo-skill", "note": "see /repo/skills/demo-skill/SKILL.md"},
    )
    assert refs == [{"name": "demo-skill", "path": "/repo/skills/demo-skill/SKILL.md"}]


def test_skill_refs_from_call_survives_unserializable_arguments():
    assert copilot_log.skill_refs_from_call("bash", {"bad": {1, 2}}) == []


def test_copilot_parse_ts_and_format_timestamp_edge_cases():
    assert copilot_log._parse_ts(None) is None
    assert copilot_log._parse_ts(123) is None
    assert copilot_log._parse_ts("not-a-timestamp") is None
    assert copilot_log.format_timestamp(None) == "(no timestamp)"
    assert copilot_log.format_timestamp(123) == "(no timestamp)"
    assert copilot_log.format_timestamp("garbage-ts") == "garbage-ts"


def test_copilot_date_only_with_no_timestamp():
    assert copilot_log.date_only(None) == "unknown"


# -- per-session events log edge cases ---------------------------------------


def test_copilot_get_state_attachments_skips_malformed_and_irrelevant_lines():
    lines = _hand_lines("malformed.jsonl")
    _write_events_log("sess-attach", lines)
    attachments = copilot_log.get_state_attachments("sess-attach")
    assert len(attachments) == 1
    rec = attachments[0]
    assert rec["display_name"] == "shot.png"
    assert rec["data_url"] == "data:image/png;base64,AAAA"
    assert rec["data"] == "AAAA"
    assert rec["media_type"] == "image/png"


def test_copilot_state_log_readers_return_empty_on_os_error(monkeypatch):
    _write_events_log("sess-oserr", ["{}"])

    def unreadable(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert copilot_log.get_state_attachments("sess-oserr") == []
    assert copilot_log.get_state_permissions("sess-oserr") == []
    assert copilot_log.get_state_skills("sess-oserr") == []


def test_copilot_permissions_from_events_skips_malformed_lines():
    assert (
        _permissions_from_events(
            sources.parse_state_records(_hand_lines("malformed.jsonl"))
        )
        == []
    )


def test_copilot_skills_from_events_skips_malformed_and_bad_data():
    assert (
        _skills_from_events(sources.parse_state_records(_hand_lines("malformed.jsonl")))
        == []
    )


def test_copilot_result_text_variants():
    assert copilot_log._result_text({"other": "x"}) == ""
    assert copilot_log._result_text("plain string") == "plain string"
    assert copilot_log._result_text(None) == ""
    assert copilot_log._result_text([1, 2, 3]) == "[1, 2, 3]"
    assert copilot_log._result_text({1, 2}) == ""


def test_copilot_attachment_lookup_skips_empty_and_captures_variants():
    _write_events_log("attachments", _hand_lines("attachment_variants.jsonl"))
    records = copilot_log.get_state_attachments("attachments")
    lookup = copilot_log._attachment_lookup(
        [{"display_name": "", "path": "", "type": "image/png"}, *records]
    )
    assert "" not in lookup
    assert lookup["url.png"]["data_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(lookup["inline.png"]["data"]) == PNG
    assert lookup["inline.png"]["media_type"] == "image/png"


# -- image byte resolution --------------------------


def test_copilot_inline_image_from_data_url():
    event = json.loads(_hand_lines("attachment_variants.jsonl")[1])
    data_url = event["data"]["attachments"][0]["dataUrl"]
    raw, ext = copilot_log._inline_image_bytes({"data_url": data_url})
    assert ext == "png"
    assert raw == PNG


def test_copilot_inline_image_bad_data_url_base64_returns_none():
    assert (
        copilot_log._inline_image_bytes({"data_url": "data:image/png;base64,A"}) is None
    )


def test_copilot_inline_image_from_inline_data_field():
    event = json.loads(_hand_lines("attachment_variants.jsonl")[2])
    attachment = event["data"]["attachments"][0]
    raw, ext = copilot_log._inline_image_bytes(
        {"data": attachment["data"], "media_type": attachment["mediaType"]}
    )
    assert ext == "png"
    assert raw == PNG


def test_copilot_inline_image_bad_inline_data_returns_none():
    assert copilot_log._inline_image_bytes({"data": "A"}) is None


def test_copilot_path_image_guesses_extension_from_type_when_no_suffix(
    tmp_path,
):
    img = tmp_path / "noext"
    img.write_bytes(PNG)
    raw = sources.read_image(str(img))
    ext = copilot_log._path_image_ext({"path": str(img), "type": "image/png"})
    assert ext == "png"
    assert raw == PNG


# -- output identifiers ------------------------------------------------------


def test_copilot_output_identifier_without_created_at_omits_date():
    assert copilot_log.output_identifier({"id": "abcdef1234"}) == "abcdef12"


def test_copilot_unique_identifier_increments_on_collision():
    used = set()
    actual_identifier = copilot_log._unique_identifier("day_id", used)
    assert actual_identifier == "day_id"
    actual_identifier = copilot_log._unique_identifier("day_id", used)
    assert actual_identifier == "day_id-2"
    actual_identifier = copilot_log._unique_identifier("day_id", used)
    assert actual_identifier == "day_id-3"


# -- session extraction integration ------------------------------------------


def test_copilot_session_prefers_events_log_tools_over_db_files(tmp_path):
    db_path = _native_db_copy(
        tmp_path,
        "INSERT INTO session_files (session_id, file_path, tool_name, turn_index) VALUES (?, ?, ?, ?)",
        (SESSION_ID, "/tmp/from_db.py", "edit", 0),
    )
    _install_native_events()
    with get_db_connection(db_path) as conn:
        session = get_session_by_id(conn, SESSION_ID)
        actual = copilot_log.extract_session(conn, session)
    expected = copilot_log.build_raw_envelope(native_source())
    assert actual == expected


# -- _extract_all --------------------------------------------------------------


def test_copilot_extract_all_reports_error_when_no_sessions(tmp_path, capsys):
    db_path = _native_db_copy(tmp_path, "DELETE FROM sessions")
    with get_db_connection(db_path) as conn:
        rc = copilot_log._extract_all(conn, None)
    assert rc == 1
    assert "no Copilot sessions" in capsys.readouterr().err


def test_copilot_extract_all_writes_one_file_per_session(tmp_path, monkeypatch, capsys):
    _install_native_events()
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "output"
    with get_db_connection(db_path) as conn:
        result = copilot_log._extract_all(conn, str(output))
    assert result == 0
    (written,) = output.iterdir()
    actual = json.loads(written.read_text())
    expected = copilot_log.build_raw_envelope(native_source())
    expected["cwd"] = str(tmp_path)
    assert written.suffix == ".json"
    assert actual == expected
    assert "done: 1/1 session(s) written" in capsys.readouterr().err


def test_copilot_extract_all_skips_session_that_fails_to_extract(
    tmp_path, monkeypatch, capsys
):
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    monkeypatch.chdir(tmp_path)

    def boom(*args, **kwargs):
        raise ValueError("extraction exploded")

    monkeypatch.setattr(copilot_log, "extract_session", boom)
    with get_db_connection(db_path) as conn:
        rc = copilot_log._extract_all(conn, str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.count("skip") == 1
    assert list(tmp_path.glob("*.json")) == []


def test_copilot_extract_all_reports_write_failure(tmp_path, monkeypatch, capsys):
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    monkeypatch.chdir(tmp_path)
    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self.suffix == ".json":
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with get_db_connection(db_path) as conn:
        rc = copilot_log._extract_all(conn, str(tmp_path), tool_result_max_bytes=-1)
    captured = capsys.readouterr()
    assert rc == 1
    assert "disk full" in captured.err


# -- main() ---------------------------------------------------------------


def test_copilot_main_reports_missing_database(tmp_path, capsys):
    rc = copilot_log.main(["--db", str(tmp_path / "missing.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in captured.err


def test_copilot_main_session_not_found_returns_error():
    rc = copilot_log.main(["bogus-id", "--db", str(DATABASE)])
    assert rc == 1


def test_copilot_main_no_newest_session_returns_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sources, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(sources, "newest_session", lambda conn, cwd, strict=False: None)
    rc = copilot_log.main(["--output", "-", "--db", str(tmp_path / "x.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no Copilot sessions" in captured.err


def test_copilot_main_reports_session_extraction_error(monkeypatch, tmp_path, capsys):
    session = {
        "id": "sess-main3",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        sources, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        sources, "newest_session", lambda conn, cwd, strict=False: session
    )

    def boom(*a, **k):
        raise ValueError("bad extraction")

    monkeypatch.setattr(copilot_log, "extract_session", boom)
    rc = copilot_log.main(["--output", "-", "--db", str(tmp_path / "x.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in captured.err


@pytest.mark.parametrize("explicit_root", [False, True])
def test_copilot_main_with_explicit_session_id_and_default_output(
    tmp_path, monkeypatch, explicit_root
):
    if not explicit_root:
        monkeypatch.chdir(tmp_path)
    status = copilot_log.main(
        [SESSION_ID, "--db", str(DATABASE)],
        project_root=tmp_path if explicit_root else None,
    )
    assert status == 0
    [output] = tmp_path.iterdir()
    assert output.name.endswith(f"_{SESSION_ID[:8]}.json")
    assert "CAPTURE_CONTEXT_SNOW_47" in output.read_text()


def test_copilot_main_with_explicit_session_id_and_output_path_writes_there(tmp_path):
    out_path = tmp_path / "custom.json"
    rc = copilot_log.main(
        [
            SESSION_ID,
            "--output",
            str(out_path),
            "--db",
            str(DATABASE),
        ]
    )
    assert rc == 0
    envelope = json.loads(out_path.read_text())
    assert "CAPTURE_CONTEXT_SNOW_47" in json.dumps(envelope)
    assert envelope["models"] == native_source().models


def test_copilot_main_reports_output_write_error(tmp_path, capsys):
    output = tmp_path / "directory.json"
    output.mkdir()
    status = copilot_log.main(
        [
            SESSION_ID,
            "--db",
            str(DATABASE),
            "--output",
            str(output),
        ]
    )
    assert status == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("selector", [True, False])
def test_copilot_cli_stdout_is_json(selector, capsys):
    _install_native_events()
    args = ["--db", str(DATABASE), "--output", "-"]
    if selector:
        args.insert(0, SESSION_ID)
    status = copilot_log.main(args)
    assert status == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["harness"] == "copilot"
    assert envelope["events"]
    assert "using session:" in captured.err


@pytest.mark.parametrize(
    "flag", [["--raw"], ["--no-raw"], ["--raw-tool-result-bytes", "3"]]
)
def test_copilot_cli_rejects_removed_flags(flag):
    with pytest.raises(SystemExit, match="2"):
        copilot_log.build_arg_parser().parse_args(flag)


def test_copilot_cli_tool_result_cap(tmp_path):
    _install_native_events()
    output = tmp_path / "session.json"
    status = copilot_log.main(
        [
            SESSION_ID,
            "--db",
            str(DATABASE),
            "--output",
            str(output),
            "--tool-result-bytes",
            "4",
        ]
    )
    assert status == 0
    envelope = json.loads(output.read_text())
    exported_calls = []
    for event in envelope["events"]:
        for call in event.get("tool_calls", []):
            exported_calls.append(call)
    call = next(iter(exported_calls))
    assert call["result_truncated"] is True
    assert call["result_bytes"] > 4
    assert len(call["result"].encode("utf-8")) <= 4
    assert list(tmp_path.glob("*.json")) == [output]


@pytest.mark.parametrize("selector", [True, False])
def test_copilot_empty_session_writes_no_json(tmp_path, capsys, selector):
    db_path = _make_test_db(tmp_path, "empty", [])
    args = ["--db", str(db_path)]
    if selector:
        args = ["empty", "--output", "-", *args]
    status = copilot_log.main(args, project_root=tmp_path / "output")
    assert status == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no exportable events" in captured.err
    assert not list(tmp_path.rglob("*.json"))


def test_copilot_db_file_fallback_preserves_names_and_targets(tmp_path):
    session_id = SESSION_ID
    target = " /tmp/a -> b.py "
    db_path = _native_db_copy(
        tmp_path,
        "INSERT INTO session_files (session_id, file_path, tool_name, turn_index) VALUES (?, ?, ?, ?)",
        (session_id, target, "edit", 0),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM session_files WHERE file_path != ?", (target,))
    with get_db_connection(db_path) as conn:
        session = get_session_by_id(conn, session_id)
        envelope = copilot_log.extract_session(conn, session)
    fallback_calls = []
    for event in envelope["events"]:
        for call in event.get("tool_calls", []):
            fallback_calls.append(call)
    [call] = fallback_calls
    assert call == {
        "name": "edit",
        "input": {"target": target},
        "result": None,
        "result_bytes": 0,
        "result_truncated": False,
    }


@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_copilot_size_warning_uses_current_flag(
    tmp_path, monkeypatch, capsys, destination
):
    monkeypatch.setattr(
        "session_log_extractors.session_log_envelope.SIZE_WARN_BYTES", 1
    )
    output = "-" if destination == "stdout" else str(tmp_path / "output.json")
    status = copilot_log.main(
        [
            SESSION_ID,
            "--db",
            str(DATABASE),
            "--output",
            output,
        ]
    )
    assert status == 0
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "--tool-result-bytes" in captured.err
    if destination == "stdout":
        assert json.loads(captured.out)["harness"] == "copilot"
    else:
        assert captured.out == ""


@pytest.mark.parametrize("cap", [0, -1])
def test_copilot_tool_result_cap_boundaries(cap):
    envelope = copilot_log.build_raw_envelope(native_tool_source(), cap)
    exported_calls = []
    for event in envelope["events"]:
        for call in event.get("tool_calls", []):
            exported_calls.append(call)
    [call] = exported_calls
    assert envelope["truncation"]["tool_result_max_bytes"] == cap
    assert call["result_truncated"] is (cap == 0)
    assert call["result_bytes"] > 0
    if cap == 0:
        assert call["result"] == ""
    else:
        assert "CAPTURE_STDOUT_OK" in call["result"]


@pytest.mark.parametrize("explicit_root", [False, True])
def test_copilot_all_default_destination_and_colliding_names(
    tmp_path, monkeypatch, explicit_root
):
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    # Duplicate only the database identity and turns to exercise output-name collision.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id,cwd,created_at,updated_at) SELECT 'collision',cwd,created_at,updated_at FROM sessions WHERE id=?",
            (SESSION_ID,),
        )
        conn.execute(
            "INSERT INTO turns (session_id,turn_index,user_message,assistant_response,timestamp) SELECT 'collision',turn_index,user_message,assistant_response,timestamp FROM turns WHERE session_id=?",
            (SESSION_ID,),
        )
    output = tmp_path / "output" if explicit_root else tmp_path
    monkeypatch.setattr(copilot_log, "output_identifier", lambda session: "same")
    monkeypatch.chdir(tmp_path)
    status = copilot_log.main(
        ["--db", str(db_path)], project_root=output if explicit_root else None
    )
    assert status == 0
    paths = sorted(output.glob("*.json"))
    assert len(paths) == 2
    assert (output / "copilot_session_log_raw_same.json").is_file()
    assert (output / "copilot_session_log_raw_same-2.json").is_file()
    written_conversations = []
    for p in paths:
        written_conversations.append(json.loads(p.read_text())["events"])
    assert all(written_conversations)


@pytest.mark.parametrize("fail", [False, True])
def test_main_closes_connection(monkeypatch, fail):
    conn = Mock()
    monkeypatch.setattr(sources, "get_db_connection", lambda path: conn)
    extract = Mock(return_value=0, side_effect=ValueError("failure") if fail else None)
    monkeypatch.setattr(copilot_log, "_extract_selected", extract)
    if fail:
        with pytest.raises(ValueError, match="failure"):
            copilot_log.main(["selected"])
    else:
        status = copilot_log.main(["selected"])
        assert status == 0
    conn.close.assert_called_once_with()


@pytest.mark.parametrize("session_id", [None, 123])
def test_output_identifier_rejects_non_string_ids(session_id):
    with pytest.raises(TypeError, match="session id must be a string"):
        copilot_log.output_identifier({"id": session_id})


def test_native_messages_keep_order_identity_and_complete_results():
    source = native_tool_source()
    # Enlarge the captured result, retaining the native call/message links.
    completion_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completion_records.append(record)
    complete = next(iter(completion_records))
    complete["data"]["result"]["content"] = "é" * 20000
    complete["data"]["result"]["additional"] = {"recorded": True}
    session = copilot_log.interpret_source(source)
    events = session["events"]
    actual_roles = []
    for event in events:
        actual_roles.append(event["role"])
    assert actual_roles == ["user", "assistant", "assistant"]
    native_messages = []
    for record in source.state_records:
        if record["type"] in ("user.message", "assistant.message"):
            native_messages.append(record)
    native = native_messages
    actual_message_ids = []
    for event in events:
        actual_message_ids.append(event["id"])
    native_message_ids = []
    for record in native:
        native_message_ids.append(record["id"])
    assert actual_message_ids == native_message_ids
    interpreted_calls = []
    for event in events:
        for call in event.get("tool_calls", []):
            interpreted_calls.append(call)
    [call] = interpreted_calls
    assert call["id"] == complete["data"]["toolCallId"]
    assert call["result"] == "é" * 20000
    assert call["result_record"]["content"] == complete["data"]["result"]
    assert "result_truncated" not in call


def test_native_repeated_or_missing_results_are_not_lost():
    source = native_tool_source()
    completion_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completion_records.append(record)
    complete = next(iter(completion_records))
    # Duplicate completion is retained as unmatched; it must not overwrite the first.
    repeated = json.loads(json.dumps(complete))
    repeated["id"] = "repeat"
    repeated["data"]["result"] = {"content": "second result"}
    source.state_records.append(repeated)
    session = copilot_log.interpret_source(source)
    calls_with_repeated_result = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            calls_with_repeated_result.append(call)
    [call] = calls_with_repeated_result
    assert "CAPTURE_STDOUT_OK" in call["result"]
    assert session["unmatched_tool_results"][0]["id"] == "repeat"
    records_without_results = []
    for record in source.state_records:
        if record["type"] != "tool.execution_complete":
            records_without_results.append(record)
    source.state_records = records_without_results
    session = copilot_log.interpret_source(source)
    calls_without_results = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            calls_without_results.append(call)
    [call] = calls_without_results
    assert call["result"] is None
    assert "result_record" not in call


def test_native_image_ownership_uses_its_record_despite_timestamp():
    source = single_image_source()
    # Misleading timestamps must not transfer the image to a later human message.
    native_users = []
    for record in source.state_records:
        if record["type"] == "user.message":
            native_users.append(record)
    user = next(iter(native_users))
    user["timestamp"] = "2099-01-01T00:00:00Z"
    later = json.loads(json.dumps(user))
    later["id"] = "later-user"
    later["data"]["messageId"] = "later-message"
    later["data"].pop("attachments")
    source.state_records.append(later)
    session = copilot_log.interpret_source(source)
    user_messages = []
    for event in session["events"]:
        if event["role"] == "user":
            user_messages.append(event)
    users = user_messages
    assert len(users) == 2
    assert base64.b64decode(users[0]["images"][0]["data"]) == PNG
    assert not users[1].get("images")


@pytest.mark.parametrize(
    "result", [None, "", {}, {"content": "", "additional": [1, False]}, [1, 2]]
)
def test_native_empty_and_structured_results_survive(result):
    source = native_tool_source()
    completion_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completion_records.append(record)
    complete = next(iter(completion_records))
    complete["data"]["result"] = result
    complete["data"]["success"] = False
    session = copilot_log.interpret_source(source)
    interpreted_calls = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            interpreted_calls.append(call)
    [call] = interpreted_calls
    assert call["result_record"]["content"] == result
    assert call["is_error"] is True
    assert call["result"] == copilot_log._result_text(result)
    assert call["result_record"]["is_error"] is True


def test_native_tool_results_match_ids_instead_of_completion_order():
    source = native_tool_source()
    native_assistants = []
    for record in source.state_records:
        if record["type"] == "assistant.message":
            native_assistants.append(record)
    request = next(iter(native_assistants))
    execution_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_start":
            execution_records.append(record)
    start = next(iter(execution_records))
    completion_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completion_records.append(record)
    complete = next(iter(completion_records))
    # Add a concurrent opaque request and finish it before the captured call.
    request["data"]["toolRequests"].append(
        {
            "toolCallId": "other",
            "name": "unknown",
            "arguments": {"nested": [1, {"keep": True}]},
        }
    )
    other = json.loads(json.dumps(complete))
    other["data"]["toolCallId"] = "other"
    other["data"]["result"] = {"content": "other output"}
    source.state_records.insert(source.state_records.index(complete), other)
    start["data"]["arguments"]["extra"] = ["execution-only"]
    session = copilot_log.interpret_source(source)
    interpreted_calls = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            interpreted_calls.append(call)
    calls = interpreted_calls
    actual_call_ids = []
    for call in calls:
        actual_call_ids.append(call["id"])
    assert actual_call_ids == [complete["data"]["toolCallId"], "other"]
    assert "CAPTURE_STDOUT_OK" in calls[0]["result"]
    assert calls[0]["execution"]["input"]["extra"] == ["execution-only"]
    assert calls[0]["execution"]["id"] == start["id"]
    assert calls[1]["result"] == "other output"
    assert calls[1]["input"] == {"nested": [1, {"keep": True}]}
    assert session["unsupported_tools"] == ["unknown"]


@pytest.mark.parametrize("parent", [True, False])
def test_native_start_without_request_uses_only_recorded_parent(parent):
    source = native_tool_source()
    native_assistants = []
    for record in source.state_records:
        if record["type"] == "assistant.message":
            native_assistants.append(record)
    request = next(iter(native_assistants))
    request["data"]["toolRequests"] = []
    if not parent:
        execution_records = []
        for record in source.state_records:
            if record["type"] == "tool.execution_start":
                execution_records.append(record)
        start = next(iter(execution_records))
        start["parentId"] = "unrecorded-message"
    session = copilot_log.interpret_source(source)
    tool_messages = []
    for event in session["events"]:
        if event.get("tool_calls"):
            tool_messages.append(event)
    tool_message = next(iter(tool_messages))
    assert (tool_message["id"] == request["id"]) is parent
    assert "CAPTURE_STDOUT_OK" in tool_message["tool_calls"][0]["result"]


def test_partial_state_keeps_database_prose_and_unowned_activity():
    source = native_tool_source()
    tool_records = []
    for record in source.state_records:
        if record["type"].startswith("tool."):
            tool_records.append(record)
    source.state_records = tool_records
    source.files = [{"file_path": "/db.py", "tool_name": "edit", "turn_index": 0}]
    session = copilot_log.interpret_source(source)
    contains_context_marker = []
    for event in session["events"]:
        contains_context_marker.append(
            "CAPTURE_CONTEXT_SNOW_47" in event.get("text", "")
        )
    assert any(contains_context_marker)
    native_tool_names = _call_names(session)
    assert native_tool_names == ["bash"]
    completion_records = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completion_records.append(record)
    source.state_records = completion_records
    session = copilot_log.interpret_source(source)
    assert session["unmatched_tool_results"][0]["tool_call_id"]
    fallback_tool_names = _call_names(session)
    assert fallback_tool_names == ["edit"]


def test_native_messages_keep_database_file_fallback_without_tool_activity():
    source = native_prose_source()
    source.files = [{"file_path": " a -> b.py ", "tool_name": "edit", "turn_index": 0}]
    session = copilot_log.interpret_source(source)
    fallback_calls = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            fallback_calls.append(call)
    [call] = fallback_calls
    assert call["input"] == {"target": " a -> b.py "}


def test_session_retains_recorded_skills_and_permission_lifecycle():
    source = native_source()
    session = copilot_log.interpret_source(source)
    assert session["skills_used"] == ["capture-greeting", "capture-command"]
    decisions = []
    feedback = []
    for permission in session["permission_decisions"]:
        decisions.append(permission["native_decision"])
        if permission["feedback"]:
            feedback.append(permission["feedback"])
    assert "approved" in decisions
    assert "denied-interactively-by-user" in decisions
    assert "cancelled" in decisions
    assert len(feedback) == 1
    assert feedback[0].startswith("CAPTURE_PERMISSION_DENY_1")


def test_owned_images_keep_distinct_paths_and_deduplicate_tokens():
    source = single_image_source()
    native_users = []
    for record in source.state_records:
        if record["type"] == "user.message":
            native_users.append(record)
    user = next(iter(native_users))
    user["data"]["content"] = "[image: pixel.png] [image:   ] compare"
    first = user["data"]["attachments"][0]
    other = {**first, "path": "/different/pixel.png"}
    # Same display name, distinct recorded source, plus a non-image attachment.
    user["data"]["attachments"].extend(
        [
            other,
            {"path": "/notes.txt", "mimeType": "text/plain", "displayName": "notes"},
        ]
    )
    session = copilot_log.interpret_source(source)
    user_messages = []
    for event in session["events"]:
        if event["role"] == "user":
            user_messages.append(event)
    [message] = user_messages
    assert len(message["images"]) == 2
    images_match_asset = []
    for image in message["images"]:
        images_match_asset.append(base64.b64decode(image["data"]) == PNG)
    assert all(images_match_asset)
    assert user["data"]["content"] == message["submitted"]["text"]
    assert user["data"]["transformedContent"] == message["text"]


def test_attachment_shape_boundaries_and_shell_input():
    assert copilot_log._attachment_ref({"path": "/tmp/shot.png"}) == {
        "name": "shot.png",
        "path": "/tmp/shot.png",
    }
    assert copilot_log._attachment_ref({"path": "/tmp/file.txt"}) is None
    assert (
        copilot_log._attachment_ref({"data_url": "data:image/png;base64,AAAA"}) is None
    )
    assert copilot_log._same_image(
        {"name": "p.png"}, {"name": "p.png", "path": "/p.png"}
    )
    source = sources.CopilotSource(
        {"id": "s"},
        [
            {
                "user_message": "<bash-input>pytest -q</bash-input>",
                "assistant_response": "ran it",
            }
        ],
    )
    session = copilot_log.interpret_source(source)
    assert session["events"][0]["shell_command"] == "pytest -q"


def test_malformed_native_tools_do_not_invent_calls():
    source = native_prose_source()
    source.state_records.extend(
        [
            {"type": "tool.execution_start", "data": {}},
            {"type": "assistant.message", "data": {"toolRequests": [False]}},
        ]
    )
    session = copilot_log.interpret_source(source)
    recorded_call_groups = []
    for event in session["events"]:
        recorded_call_groups.append(event.get("tool_calls"))
    assert not any(recorded_call_groups)


def test_native_tools_preserve_recorded_database_file_activity():
    source = native_tool_source()
    source.files = [
        {
            "file_path": "/tmp/file.py",
            "tool_name": "view",
            "turn_index": 0,
            "first_seen_at": "recorded time",
        }
    ]
    session = copilot_log.interpret_source(source)
    assert session["file_activity"] == [
        {
            "file_path": "/tmp/file.py",
            "tool_name": "view",
            "provenance": {
                "record_type": "copilot.session_files",
                "turn_index": 0,
                "first_seen_at": "recorded time",
            },
        }
    ]
    actual_tool_names = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            actual_tool_names.append(call["name"])
    assert actual_tool_names == ["bash"]


@pytest.mark.parametrize(
    "name", ["Bash", "Read", "Skill", "AskUserQuestion", "ExitPlanMode"]
)
def test_copilot_reports_claude_tool_names_as_unsupported(name):
    source = native_tool_source()
    native_assistants = []
    for record in source.state_records:
        if record["type"] == "assistant.message":
            native_assistants.append(record)
    request = next(iter(native_assistants))["data"]["toolRequests"][0]
    # Rename only the native tool request; complete input and call links must survive.
    request["name"] = name
    session = copilot_log.interpret_source(source)
    interpreted_calls = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            interpreted_calls.append(call)
    [call] = interpreted_calls
    assert call["input_schema"] == "unsupported"
    assert session["unsupported_tools"] == [name]
    assert call["input"] == request["arguments"]
    assert "CAPTURE_STDOUT_OK" in call["result"]


@pytest.mark.parametrize("native_kind", ["pending", "future-policy-result", None])
def test_permission_completion_status_is_separate_from_native_label(native_kind):
    source = native_prose_source()
    permissions = sources.parse_state_records(_hand_lines("permissions.jsonl"))
    # Change only a completed result's label, including one resembling a lifecycle state.
    permissions[1]["data"]["result"]["kind"] = native_kind
    source.state_records.extend(permissions)
    session = copilot_log.interpret_source(source)
    first, _, pending = session["permission_decisions"]
    assert first["status"] == "completed"
    assert first["native_decision"] == native_kind
    assert first["request_id"] == "r1"
    assert pending["status"] == "pending"
    assert pending["native_decision"] is None
    assert pending["request_id"] == "r3"
    has_permission_denial = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            has_permission_denial.append("permission_denial" in call)
    assert not any(has_permission_denial)


def test_file_activity_keeps_database_fields_as_provenance():
    source = native_tool_source()
    # This native database index deliberately differs from all shared message indices.
    source.files = [
        {
            "file_path": "/a.py",
            "tool_name": "view",
            "turn_index": 99,
            "first_seen_at": "recorded time",
        }
    ]
    session = copilot_log.interpret_source(source)
    [activity] = session["file_activity"]
    assert activity["file_path"] == "/a.py"
    assert activity["tool_name"] == "view"
    assert activity["provenance"] == {
        "record_type": "copilot.session_files",
        "turn_index": 99,
        "first_seen_at": "recorded time",
    }
    assert "turn_index" not in activity
    assert "files_changed" not in session


@pytest.mark.parametrize("request_id", [None, "r1", "orphan"])
def test_permission_completion_retains_request_identity_and_timestamp_fallback(
    request_id,
):
    records = sources.parse_state_records(_hand_lines("permissions.jsonl"))[:2]
    # Remove completion time and vary only its recorded request ID.
    records[1].pop("timestamp")
    records[1]["data"]["requestId"] = request_id
    permissions = copilot_log._permissions_from_events(records)
    completed_permissions = []
    for record in permissions:
        if record["status"] == "completed":
            completed_permissions.append(record)
    completion = next(iter(completed_permissions))
    assert completion["request_id"] == request_id
    assert completion["native_decision"] == "approved"
    assert completion["ts"] == (records[0]["timestamp"] if request_id == "r1" else None)
    assert completion["tool"] == ("pytest" if request_id == "r1" else "tool")
    is_pending = []
    for record in permissions:
        is_pending.append(record["status"] == "pending")
    assert any(is_pending) is (request_id != "r1")


def test_native_result_without_execution_start_does_not_invent_one():
    source = native_tool_source()
    records_without_starts = []
    for record in source.state_records:
        if record["type"] != "tool.execution_start":
            records_without_starts.append(record)
    source.state_records = records_without_starts
    session = copilot_log.interpret_source(source)
    interpreted_calls = []
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            interpreted_calls.append(call)
    [call] = interpreted_calls
    assert "execution" not in call
    assert "CAPTURE_STDOUT_OK" in call["result"]
