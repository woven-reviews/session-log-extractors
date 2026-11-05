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
    install_native_events,
    native_db_copy,
    native_lines,
    native_prose_source,
    native_source,
    native_tool_source,
    recorded_calls,
    single_image_source,
    write_events_log,
)

from session_log_extractors import copilot_protocol
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


def _call_names(session):
    names = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            names.append(call["name"])
    return names


def _native_db():
    return get_db_connection(DATABASE)


def _hand_lines(name: str) -> list[str]:
    return (FIXTURES / "boundary" / name).read_text(encoding="utf-8").splitlines()


@pytest.fixture(autouse=True)
def _isolate_copilot_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path / "session-state")


def _make_test_db(tmp_path, session_id, turns):
    """Copy the native DB schema with one session holding the given turns."""
    db_path = native_db_copy(tmp_path)
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
    install_native_events(sources.COPILOT_STATE_ROOT)
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
    records = sources.parse_state_records(native_lines())
    starts = []
    resumes = []
    for record in records:
        if record["type"] == copilot_protocol.SESSION_START:
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
    install_native_events(sources.COPILOT_STATE_ROOT)
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
        if record["type"] != copilot_protocol.BINARY_ASSET:
            lines.append(json.dumps(record))
    write_events_log(sources.COPILOT_STATE_ROOT, "sess-asset", lines)
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
    actual_decode_arguments = copilot_log._decode_arguments('{"a": 1}')
    assert actual_decode_arguments == {"a": 1}
    actual_decode_arguments = copilot_log._decode_arguments("not json")
    assert actual_decode_arguments == "not json"
    actual_decode_arguments = copilot_log._decode_arguments({"a": 1})
    assert actual_decode_arguments == {"a": 1}


def test_skill_refs_from_call_finds_named_skill_and_fills_in_its_path():
    # The "skill" call registers the name with no path; the SKILL.md regex
    # match for the same name later fills it in.
    refs = copilot_log.skill_refs_from_call(
        "builtin.skill",
        {"skill": "demo-skill", "note": "see /repo/skills/demo-skill/SKILL.md"},
    )
    assert refs == [{"name": "demo-skill", "path": "/repo/skills/demo-skill/SKILL.md"}]


def test_skill_refs_from_call_survives_unserializable_arguments():
    actual_skill_refs_from_call = copilot_log.skill_refs_from_call(
        "bash", {"bad": {1, 2}}
    )
    assert actual_skill_refs_from_call == []


def test_copilot_parse_ts_and_format_timestamp_edge_cases():
    actual_parse_ts = copilot_log._parse_ts(None)
    assert actual_parse_ts is None
    actual_parse_ts = copilot_log._parse_ts(123)
    assert actual_parse_ts is None
    actual_parse_ts = copilot_log._parse_ts("not-a-timestamp")
    assert actual_parse_ts is None
    actual_format_timestamp = copilot_log.format_timestamp(None)
    assert actual_format_timestamp == "(no timestamp)"
    actual_format_timestamp = copilot_log.format_timestamp(123)
    assert actual_format_timestamp == "(no timestamp)"
    actual_format_timestamp = copilot_log.format_timestamp("garbage-ts")
    assert actual_format_timestamp == "garbage-ts"


def test_copilot_date_only_with_no_timestamp():
    actual_date_only = copilot_log.date_only(None)
    assert actual_date_only == "unknown"


# -- per-session events log edge cases ---------------------------------------


def test_copilot_get_state_attachments_skips_malformed_and_irrelevant_lines():
    lines = _hand_lines("malformed.jsonl")
    write_events_log(sources.COPILOT_STATE_ROOT, "sess-attach", lines)
    attachments = copilot_log.get_state_attachments("sess-attach")
    assert len(attachments) == 1
    rec = attachments[0]
    assert rec["display_name"] == "shot.png"
    assert rec["data_url"] == "data:image/png;base64,AAAA"
    assert rec["data"] == "AAAA"
    assert rec["media_type"] == "image/png"


def test_copilot_state_log_readers_propagate_os_error(monkeypatch):
    write_events_log(sources.COPILOT_STATE_ROOT, "sess-oserr", ["{}"])

    def unreadable(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "open", unreadable)
    for reader in (
        copilot_log.get_state_attachments,
        copilot_log.get_state_permissions,
        copilot_log.get_state_skills,
    ):
        with pytest.raises(OSError, match="boom"):
            reader("sess-oserr")


def test_copilot_permissions_from_events_skips_malformed_lines():
    actual_parse_state_records = sources.parse_state_records(
        _hand_lines("malformed.jsonl")
    )
    actual_permissions_from_events = _permissions_from_events(
        actual_parse_state_records
    )
    assert actual_permissions_from_events == []


def test_copilot_skills_from_events_skips_malformed_and_bad_data():
    actual_parse_state_records = sources.parse_state_records(
        _hand_lines("malformed.jsonl")
    )
    actual_skills_from_events = _skills_from_events(actual_parse_state_records)
    assert actual_skills_from_events == []


def test_copilot_result_text_variants():
    actual_result_text = copilot_log._result_text({"other": "x"})
    assert actual_result_text == ""
    actual_result_text = copilot_log._result_text("plain string")
    assert actual_result_text == "plain string"
    actual_result_text = copilot_log._result_text(None)
    assert actual_result_text == ""
    actual_result_text = copilot_log._result_text([1, 2, 3])
    assert actual_result_text == "[1, 2, 3]"
    actual_result_text = copilot_log._result_text({1, 2})
    assert actual_result_text == ""


def test_copilot_attachment_lookup_skips_empty_and_captures_variants():
    write_events_log(
        sources.COPILOT_STATE_ROOT,
        "attachments",
        _hand_lines("attachment_variants.jsonl"),
    )
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
    actual_inline_image_bytes = copilot_log._inline_image_bytes(
        {"data_url": "data:image/png;base64,A"}
    )
    assert actual_inline_image_bytes is None


def test_copilot_inline_image_from_inline_data_field():
    event = json.loads(_hand_lines("attachment_variants.jsonl")[2])
    attachment = event["data"]["attachments"][0]
    raw, ext = copilot_log._inline_image_bytes(
        {"data": attachment["data"], "media_type": attachment["mediaType"]}
    )
    assert ext == "png"
    assert raw == PNG


def test_copilot_inline_image_bad_inline_data_returns_none():
    actual_inline_image_bytes = copilot_log._inline_image_bytes({"data": "A"})
    assert actual_inline_image_bytes is None


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
    actual_output_identifier = copilot_log.output_identifier({"id": "abcdef1234"})
    assert actual_output_identifier == "abcdef12"


# -- session extraction integration ------------------------------------------


def test_copilot_session_prefers_events_log_tools_over_db_files(tmp_path):
    db_path = native_db_copy(
        tmp_path,
        "INSERT INTO session_files (session_id, file_path, tool_name, turn_index) VALUES (?, ?, ?, ?)",
        (SESSION_ID, "/tmp/from_db.py", "edit", 0),
    )
    install_native_events(sources.COPILOT_STATE_ROOT)
    with get_db_connection(db_path) as conn:
        session = get_session_by_id(conn, SESSION_ID)
        actual = copilot_log.extract_session(conn, session)
    expected = copilot_log.build_raw_envelope(native_source())
    assert actual == expected


# -- _extract_all --------------------------------------------------------------


def test_copilot_extract_all_reports_error_when_no_sessions(tmp_path, capsys):
    db_path = native_db_copy(tmp_path, "DELETE FROM sessions")
    with get_db_connection(db_path) as conn:
        rc = copilot_log._extract_all(conn, None)
    assert rc == 1
    captured = capsys.readouterr()
    assert "no Copilot sessions" in captured.err


def test_copilot_extract_all_writes_one_file_per_session(tmp_path, monkeypatch, capsys):
    install_native_events(sources.COPILOT_STATE_ROOT)
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
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
    captured = capsys.readouterr()
    assert "done: 1/1 session(s) written" in captured.err


def test_copilot_extract_all_skips_session_that_fails_to_extract(
    tmp_path, monkeypatch, capsys
):
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
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
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
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
    captured = capsys.readouterr()
    assert status == 1
    assert "error:" in captured.err


@pytest.mark.parametrize("selector", [True, False])
def test_copilot_cli_stdout_is_json(selector, capsys):
    install_native_events(sources.COPILOT_STATE_ROOT)
    args = ["--db", str(DATABASE), "--output", "-"]
    if selector:
        args.insert(0, SESSION_ID)
    status = copilot_log.main(args)
    captured = capsys.readouterr()
    assert status == 0
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
    install_native_events(sources.COPILOT_STATE_ROOT)
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
    truncated = []
    for call in exported_calls:
        result = call["result"]
        if result is None:
            assert call["result_bytes"] == 0
            assert call["result_truncated"] is False
            continue
        assert isinstance(result, str)
        assert len(result.encode("utf-8")) <= 4
        if call["result_truncated"]:
            assert call["result_bytes"] > 4
            truncated.append(call)
    assert truncated, "Required native result exceeding the byte cap is missing"
    assert list(tmp_path.glob("*.json")) == [output]


@pytest.mark.parametrize("destination", ["stdout", "file", "default", "batch"])
@pytest.mark.parametrize("contents", [None, "", "  \n\t", "{}\n42\n"])
def test_copilot_empty_session_exports_json(tmp_path, capsys, destination, contents):
    db_path = _make_test_db(tmp_path, "empty", [])
    if contents is not None:
        write_events_log(sources.COPILOT_STATE_ROOT, "empty", [contents])
    output = tmp_path / "output"
    output.mkdir()
    args = ["--db", str(db_path)]
    if destination != "batch":
        args.insert(0, "empty")
    if destination == "stdout":
        args.extend(["--output", "-"])
    elif destination == "file":
        args.extend(["--output", str(output / "custom.json")])

    status = copilot_log.main(args, project_root=output)

    captured = capsys.readouterr()
    assert status == 0
    if destination == "stdout":
        envelope = json.loads(captured.out)
        assert list(output.iterdir()) == []
    else:
        assert captured.out == ""
        [written] = output.iterdir()
        envelope = json.loads(written.read_text())
    assert envelope["events"] == []
    assert envelope["harness"] == "copilot"
    assert envelope["cwd"] == str(Path.cwd())
    assert envelope["started_at"] == "2026-01-01T12:00:00Z"


def test_copilot_db_file_fallback_preserves_names_and_targets(tmp_path):
    session_id = SESSION_ID
    target = " /tmp/a -> b.py "
    db_path = native_db_copy(
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
    captured = capsys.readouterr()
    assert status == 0
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
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
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
        status = copilot_log.main(["selected"])
        assert status == 1
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
    # Enlarge the captured result, retaining the native_messages call/message links.
    completion_records = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
        if record["type"] in (
            copilot_protocol.USER_MESSAGE,
            copilot_protocol.ASSISTANT_MESSAGE,
        ):
            native_messages.append(record)
    actual_message_ids = []
    for event in events:
        actual_message_ids.append(event["id"])
    native_message_ids = []
    for record in native_messages:
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
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
        if record["type"] != copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
        if record["type"] == copilot_protocol.USER_MESSAGE:
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
    assert len(user_messages) == 2
    assert base64.b64decode(user_messages[0]["images"][0]["data"]) == PNG
    assert not user_messages[1].get("images")


@pytest.mark.parametrize(
    "result", [None, "", {}, {"content": "", "additional": [1, False]}, [1, 2]]
)
def test_native_empty_and_structured_results_survive(result):
    source = native_tool_source()
    completion_records = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
    actual_result_text = copilot_log._result_text(result)
    assert call["result"] == actual_result_text
    assert call["result_record"]["is_error"] is True


def test_native_tool_results_match_ids_instead_of_completion_order():
    source = native_tool_source()
    native_assistants = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            native_assistants.append(record)
    request = next(iter(native_assistants))
    execution_records = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.TOOL_EXECUTION_START:
            execution_records.append(record)
    start = next(iter(execution_records))
    completion_records = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
    actual_call_ids = []
    for call in interpreted_calls:
        actual_call_ids.append(call["id"])
    assert actual_call_ids == [complete["data"]["toolCallId"], "other"]
    assert "CAPTURE_STDOUT_OK" in interpreted_calls[0]["result"]
    assert interpreted_calls[0]["execution"]["input"]["extra"] == ["execution-only"]
    assert interpreted_calls[0]["execution"]["id"] == start["id"]
    assert interpreted_calls[1]["result"] == "other output"
    assert interpreted_calls[1]["input"] == {"nested": [1, {"keep": True}]}
    assert session["unsupported_tools"] == ["unknown"]


@pytest.mark.parametrize("parent", [True, False])
def test_native_start_without_request_uses_only_recorded_parent(parent):
    source = native_tool_source()
    native_assistants = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            native_assistants.append(record)
    request = next(iter(native_assistants))
    request["data"]["toolRequests"] = []
    if not parent:
        execution_records = []
        for record in source.state_records:
            if record["type"] == copilot_protocol.TOOL_EXECUTION_START:
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
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
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
        if record["type"] == copilot_protocol.USER_MESSAGE:
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


def test_empty_transformation_retains_submitted_user_input():
    source = native_prose_source()
    users = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.USER_MESSAGE:
            users.append(record)
    [user] = users
    # Keep only the native input and record an empty transformation, without attachments.
    user["data"]["transformedContent"] = ""
    user["data"]["attachments"] = []
    source.state_records = [user]
    submitted_text = user["data"]["content"]
    assert submitted_text

    session = copilot_log.interpret_source(source)

    assert session is not None
    [message] = session["events"]
    assert message["role"] == "user"
    assert message["text"] == ""
    assert message["submitted"]["text"] == submitted_text


def test_attachment_shape_boundaries_and_shell_input():
    actual_attachment_ref = copilot_log._attachment_ref({"path": "/tmp/shot.png"})
    assert actual_attachment_ref == {"name": "shot.png", "path": "/tmp/shot.png"}
    actual_attachment_ref = copilot_log._attachment_ref({"path": "/tmp/file.txt"})
    assert actual_attachment_ref is None
    actual_attachment_ref = copilot_log._attachment_ref(
        {"data_url": "data:image/png;base64,AAAA"}
    )
    assert actual_attachment_ref is None
    actual_same_image = copilot_log._same_image(
        {"name": "p.png"}, {"name": "p.png", "path": "/p.png"}
    )
    assert actual_same_image
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
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
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
        if record["type"] != copilot_protocol.TOOL_EXECUTION_START:
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


def test_response_only_results_preserve_supplemental_file_activity_order():
    source = native_source()
    users = []
    responses = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.USER_MESSAGE:
            users.append(record)
        if (
            record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED
            and record["data"]["response"]["responseKind"] == copilot_protocol.ASK_USER
        ):
            responses.append(record)
    assert users and responses and source.files, (
        "Required native fallback evidence is missing"
    )
    source.state_records = [users[0], responses[0]]
    source.files = source.files[:1]

    session = copilot_log.interpret_source(source)

    user, response, file_activity = session["events"]
    assert user["id"] == users[0]["id"]
    assert response["id"] == responses[0]["id"]
    assert (
        file_activity["tool_calls"][0]["input"]["target"]
        == source.files[0]["file_path"]
    )


@pytest.mark.parametrize("destination", ["stdout", "file", "batch"])
@pytest.mark.parametrize("contents", ["bad\n" * 7, "42\n[]\n", "directory"])
def test_copilot_cli_rejects_invalid_existing_sidecar(
    tmp_path, capsys, destination, contents
):
    # Keep the populated native database: corrupt sidecars must not use fallback.
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(Path.cwd()),))
    path = write_events_log(sources.COPILOT_STATE_ROOT, SESSION_ID, [contents])
    if contents == "directory":
        path.unlink()
        path.mkdir()
    output = tmp_path / "output"
    args = ["--db", str(db_path)]
    if destination != "batch":
        args.insert(0, SESSION_ID)
        args.extend(["--output", "-" if destination == "stdout" else str(output)])

    status = copilot_log.main(args, project_root=output)

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert str(path) in captured.err
    assert not list(tmp_path.rglob("*.json"))
    if contents != "directory":
        assert "no parseable JSON entries" in captured.err


@pytest.mark.parametrize("destination", ["stdout", "file", "batch"])
def test_copilot_cli_reports_unreadable_sidecar(
    tmp_path, monkeypatch, capsys, destination
):
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(Path.cwd()),))
    path = write_events_log(sources.COPILOT_STATE_ROOT, SESSION_ID, native_lines())
    original_open = Path.open

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError(f"access denied: {path}")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    output = tmp_path / "output"
    args = ["--db", str(db_path)]
    if destination != "batch":
        args.insert(0, SESSION_ID)
        args.extend(["--output", "-" if destination == "stdout" else str(output)])
    status = copilot_log.main(args, project_root=output)
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "access denied" in captured.err
    assert str(path) in captured.err


def test_copilot_cli_preserves_native_content_with_bounded_diagnostics(capsys):
    expected = copilot_log.build_raw_envelope(native_source())
    # Retain the complete capture, adding blank and malformed/non-object lines.
    path = write_events_log(
        sources.COPILOT_STATE_ROOT,
        SESSION_ID,
        ["", "42", *native_lines(), *(["bad"] * 6)],
    )
    status = copilot_log.main([SESSION_ID, "--db", str(DATABASE), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out) == expected
    warnings = []
    for line in captured.err.splitlines():
        if line.startswith("warning:"):
            warnings.append(line)
    assert len(warnings) == 6
    assert f"{path}: skipping non-object JSON on line 2" in warnings[0]
    assert "skipped 7 malformed/non-object records total" in warnings[-1]


@pytest.mark.parametrize("contents", [None, "", " \n", "{}\n"])
def test_copilot_cli_preserves_database_fallback(contents, capsys):
    with get_db_connection(DATABASE) as conn:
        metadata = get_session_by_id(conn, SESSION_ID)
        expected = copilot_log.extract_session(conn, metadata)
    if contents is not None:
        write_events_log(sources.COPILOT_STATE_ROOT, SESSION_ID, [contents])
    status = copilot_log.main([SESSION_ID, "--db", str(DATABASE), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 0
    actual = json.loads(captured.out)
    assert actual == expected
    assert actual["events"]
    assert "warning:" not in captured.err


@pytest.mark.parametrize("failure", ["sidecar", "database", "output"])
def test_copilot_batch_attempts_all_and_retains_successes(
    tmp_path, monkeypatch, capsys, failure
):
    db_path = _make_test_db(tmp_path, "first", [])
    # Duplicate only session identity; all three are valid empty sessions.
    with sqlite3.connect(db_path) as conn:
        for session_id in ("broken", "last"):
            conn.execute(
                "INSERT INTO sessions (id,cwd,created_at,updated_at) "
                "SELECT ?,cwd,created_at,updated_at FROM sessions WHERE id='first'",
                (session_id,),
            )
    if failure == "sidecar":
        write_events_log(sources.COPILOT_STATE_ROOT, "broken", ["bad"])
    elif failure == "database":
        original = sources.get_session_turns

        def failed_turns(conn, session_id):
            if session_id == "broken":
                raise sqlite3.OperationalError("cannot read turns")
            return original(conn, session_id)

        monkeypatch.setattr(sources, "get_session_turns", failed_turns)
    output = tmp_path / "output"
    output.mkdir()
    broken_path = output / "copilot_session_log_raw_2026-01-01_broken.json"
    if failure == "output":
        broken_path.mkdir()
    status = copilot_log.main(["--db", str(db_path)], project_root=output)
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "done: 2/3 session(s) written" in captured.err
    for session_id in ("first", "last"):
        path = output / f"copilot_session_log_raw_2026-01-01_{session_id}.json"
        assert json.loads(path.read_text())["events"] == []
    assert not broken_path.is_file()


@pytest.mark.parametrize("mode", ["selected", "newest", "batch"])
@pytest.mark.parametrize("problem", ["corrupt", "schema", "turns", "unreadable"])
def test_copilot_cli_reports_database_failure(
    tmp_path, monkeypatch, capsys, mode, problem
):
    db_path = tmp_path / "input.db"
    if problem == "corrupt":
        db_path.write_bytes(b"not sqlite")
    elif problem == "schema":
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT)")
    elif problem == "turns":
        db_path = native_db_copy(
            tmp_path, "UPDATE sessions SET cwd=?", (str(Path.cwd()),)
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE turns")
    else:

        def denied(path):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(sources, "get_db_connection", denied)
    args = ["--db", str(db_path)]
    if mode != "batch":
        args.extend(["--output", "-"])
    if mode == "selected":
        args.insert(0, SESSION_ID)
    status = copilot_log.main(args, project_root=tmp_path / "output")
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "error:" in captured.err or "skip" in captured.err
    assert not list(tmp_path.rglob("*.json"))


def test_copilot_cli_reports_output_directory_failure(tmp_path, capsys):
    db_path = _make_test_db(tmp_path, "empty", [])
    output = tmp_path / "file"
    output.touch()
    status = copilot_log.main(["--db", str(db_path)], project_root=output)
    captured = capsys.readouterr()
    assert status == 1
    assert "error:" in captured.err
    assert captured.out == ""


def test_copilot_cli_help_explains_outcomes(capsys):
    with pytest.raises(SystemExit, match="0"):
        copilot_log.main(["--help"])
    help_text = capsys.readouterr().out
    assert "events: []" in help_text
    assert "exit 1" in help_text
    assert "malformed/non-object" in help_text


def test_copilot_cli_reports_stdout_failure(monkeypatch, capsys):
    output = Mock()
    output.write.side_effect = BrokenPipeError("closed stdout")
    monkeypatch.setattr(copilot_log.sys, "stdout", output)
    status = copilot_log.main([SESSION_ID, "--db", str(DATABASE), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 1
    assert "closed stdout" in captured.err


def test_copilot_batch_without_selected_sessions_fails(tmp_path, capsys):
    db_path = native_db_copy(tmp_path, "DELETE FROM sessions")
    status = copilot_log.main(["--db", str(db_path)], project_root=tmp_path / "output")
    captured = capsys.readouterr()
    assert status == 1
    assert "no Copilot sessions" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "output").exists()


def test_copilot_batch_exports_populated_and_empty_sessions(tmp_path, capsys):
    db_path = native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(Path.cwd()),))
    # Add one identity without turns; retain the populated native session intact.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id,cwd,created_at) SELECT 'empty',cwd,NULL FROM sessions WHERE id=?",
            (SESSION_ID,),
        )
    install_native_events(sources.COPILOT_STATE_ROOT)
    output = tmp_path / "output"
    status = copilot_log.main(["--db", str(db_path)], project_root=output)
    captured = capsys.readouterr()
    assert status == 0
    assert "done: 2/2" in captured.err
    assert captured.out == ""
    paths = sorted(output.iterdir())
    assert len(paths) == 2
    empty = json.loads((output / "copilot_session_log_raw_empty.json").read_text())
    assert empty["events"] == []
    populated_paths = []
    for path in paths:
        if path.name != "copilot_session_log_raw_empty.json":
            populated_paths.append(path)
    [populated_path] = populated_paths
    actual = json.loads(populated_path.read_text())
    expected = copilot_log.build_raw_envelope(native_source())
    expected["cwd"] = str(Path.cwd())
    assert actual == expected


@pytest.mark.parametrize("batch", [False, True])
def test_copilot_cli_rejects_non_string_database_identity(tmp_path, capsys, batch):
    db_path = native_db_copy(tmp_path)
    # Remove SQLite's TEXT affinity to simulate an incompatible required schema.
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE sessions RENAME TO original_sessions")
        conn.execute(
            "CREATE TABLE sessions (id,cwd,repository,branch,summary,created_at,updated_at)"
        )
        conn.execute(
            "INSERT INTO sessions SELECT 42,?,repository,branch,summary,created_at,updated_at "
            "FROM original_sessions",
            (str(Path.cwd()),),
        )
    args = ["--db", str(db_path)]
    if not batch:
        args.extend(["42", "--output", "-"])
    status = copilot_log.main(args, project_root=tmp_path / "output")
    captured = capsys.readouterr()
    assert status == 1
    assert "session id must be a string" in captured.err
    assert captured.out == ""
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("mode", ["stdout", "explicit", "default", "batch"])
def test_full_copilot_identity_in_all_output_modes(mode, tmp_path, capsys):
    db_path = native_db_copy(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET cwd = ?", (str(Path.cwd()),))
    with get_db_connection(db_path) as conn:
        recorded = get_session_by_id(conn, SESSION_ID)
    assert recorded is not None
    destination = tmp_path / "explicit.json"
    args = [SESSION_ID, "--db", str(db_path)]
    if mode == "stdout":
        args += ["--output", "-"]
    elif mode == "explicit":
        args += ["--output", str(destination)]
    elif mode == "batch":
        args = ["--all", "--db", str(db_path)]
    status = copilot_log.main(args, project_root=tmp_path)
    captured = capsys.readouterr()
    assert status == 0, captured.err
    if mode in {"default", "batch"}:
        label = copilot_log.output_identifier(recorded)
        destination = tmp_path / f"copilot_session_log_raw_{label}.json"
    payload = captured.out if mode == "stdout" else destination.read_text()
    assert json.loads(payload)["session_id"] == SESSION_ID


def test_copilot_prefix_collision_keeps_full_identities(tmp_path, capsys):
    db_path = native_db_copy(tmp_path)
    # Duplicate just the native session row, holding date and prefix constant.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET id = ?, cwd = ?, created_at = ?",
            ("abcdefgh-1111", str(Path.cwd()), "2026-01-01T12:00:00Z"),
        )
        conn.execute(
            "INSERT INTO sessions (id, cwd, created_at) VALUES (?, ?, ?)",
            ("abcdefgh-2222", str(Path.cwd()), "2026-01-01T12:00:00Z"),
        )
    status = copilot_log.main(["--all", "--db", str(db_path)], project_root=tmp_path)
    captured = capsys.readouterr()
    assert status == 0, captured.err
    identities = set()
    names = set()
    for path in tmp_path.glob("*.json"):
        names.add(path.name)
        identities.add(json.loads(path.read_text())["session_id"])
    assert identities == {"abcdefgh-1111", "abcdefgh-2222"}
    assert names == {
        "copilot_session_log_raw_2026-01-01_abcdefgh.json",
        "copilot_session_log_raw_2026-01-01_abcdefgh-2.json",
    }


@pytest.mark.parametrize(
    ("native_id", "expected"), [(None, "2026-01-01_unknown"), ("", "2026-01-01_")]
)
def test_missing_copilot_identity_preserves_fallback(native_id, expected):
    source = native_source()
    source.session["created_at"] = "2026-01-01T12:00:00Z"
    if native_id is None:
        source.session.pop("id")
    else:
        source.session["id"] = native_id
    session = copilot_log.interpret_source(source)
    assert session["session_id"] == expected


def test_malformed_copilot_identity_keeps_type_error():
    source = native_source()
    source.session["id"] = 17
    with pytest.raises(TypeError):
        copilot_log.interpret_source(source)
