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
    clean_user_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "copilot"
NATIVE_IDS = json.loads((FIXTURES / "manifest.json").read_text())["cases"]
PNG = (FIXTURES / "pixel.png").read_bytes()


def _native_lines(case: str) -> list[str]:
    return (FIXTURES / case / "events.jsonl").read_text(encoding="utf-8").splitlines()


def _native_db():
    return get_db_connection(FIXTURES / "session-store.db")


def _native_db_copy(tmp_path: Path, sql: str = "", params=()) -> Path:
    """Copy the native DB into tmp_path, optionally applying one edit."""
    db_path = tmp_path / "session-store.db"
    shutil.copyfile(FIXTURES / "session-store.db", db_path)
    if sql:
        with sqlite3.connect(db_path) as conn:
            conn.execute(sql, params)
    return db_path


def _install_native_events(case: str) -> None:
    _write_events_log(NATIVE_IDS[case], _native_lines(case))


def _hand_lines(name: str) -> list[str]:
    return (FIXTURES / "hand-authored" / name).read_text(encoding="utf-8").splitlines()


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
        for t in turns:
            conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, "
                "assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    t["turn_index"],
                    t["user_message"],
                    t["assistant_response"],
                    t["timestamp"],
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


def test_copilot_noise_cleaning():
    assert clean_user_text("<system-notification>test</system-notification>") == ""
    assert clean_user_text("<bash-stdout>output</bash-stdout>") == ""
    assert (
        clean_user_text(
            "Real text\n<system-reminder>ignore</system-reminder>\nMore text"
        )
        == "Real text\n\nMore text"
    )


def test_copilot_permissions_join_approval_denial_and_pending():
    perms = _permissions_from_events(
        sources.parse_state_records(_hand_lines("permissions.jsonl"))
    )
    assert [
        (p["tool"], p["decision"], p["feedback"], p["timestamp"]) for p in perms
    ] == [
        ("pytest", "approved", "", "2026-07-01T22:04:04Z"),
        (
            "edit crud.py",
            "denied-interactively-by-user",
            "not like that",
            "2026-07-01T22:05:30Z",
        ),
        ("rm x", "pending", "", "2026-07-01T22:06:00Z"),
    ]


def test_copilot_db_operations():
    _install_native_events("continued")
    with _native_db() as conn:
        session = get_session_by_id(conn, NATIVE_IDS["continued"])
        assert session["cwd"] == "/workspace"
        turns = get_session_turns(conn, session["id"])
        assert [t["assistant_response"] for t in turns] == ["READY", "BLUE-WREN-17"]
        assert any(
            row["id"] == session["id"]
            for row in matching_sessions(conn, Path("/workspace"), strict=True)
        )
        envelope = copilot_log.extract_session(conn, session)
    assert [e["text"] for e in envelope["events"] if e["role"] == "assistant"] == [
        "READY",
        "BLUE-WREN-17",
    ]
    assert envelope["cwd"] == "/workspace"
    assert envelope["started_at"] == session["created_at"]
    assert envelope["models"] == ["claude-sonnet-5"]


def test_copilot_native_fixture_ids_match_db_and_events():
    with _native_db() as conn:
        assert {row["id"] for row in conn.execute("SELECT id FROM sessions")} == set(
            NATIVE_IDS.values()
        )
        for case, session_id in NATIVE_IDS.items():
            events = [json.loads(line) for line in _native_lines(case)]
            assert events[0]["type"] == "session.start"
            assert events[0]["data"]["sessionId"] == session_id
            assert sum(event["type"] == "user.message" for event in events) == len(
                get_session_turns(conn, session_id)
            )


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
    shutil.copyfile(FIXTURES / "pixel.png", img_path)
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
    assert not Path("/workspace/pixel.png").exists()
    _install_native_events("image")
    with _native_db() as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, NATIVE_IDS["image"])
        )
    user = next(e for e in envelope["events"] if e["role"] == "user")
    assert base64.b64decode(user["images"][0]["data"]) == PNG
    assert "description pending" not in user["text"]


def test_copilot_attachment_without_its_binary_asset_keeps_the_path():
    lines = [
        line for line in _native_lines("image") if '"session.binary_asset"' not in line
    ]
    _write_events_log("sess-asset", lines)
    [record] = copilot_log.get_state_attachments("sess-asset")
    assert record["path"] == "/workspace/pixel.png"
    assert "data" not in record


def test_copilot_native_tool_success_keeps_full_call():
    _install_native_events("tool_success")
    with _native_db() as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, NATIVE_IDS["tool_success"])
        )
    calls = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
    assert len(calls) == 1
    assert calls[0]["name"] == "bash"
    assert calls[0]["input"]["command"] == "printf TOOL_FIXTURE_MARKER"
    assert "TOOL_FIXTURE_MARKER" in calls[0]["result"]


def test_copilot_native_tool_failure_keeps_shell_exit_code():
    _install_native_events("tool_failure")
    with _native_db() as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, NATIVE_IDS["tool_failure"])
        )
    calls = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
    assert len(calls) == 1
    assert calls[0]["input"]["command"] == "false"
    assert "exit code 1" in calls[0]["result"]


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


def test_copilot_clean_user_text_empty_input_returns_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


# -- timestamp helpers ------------------------------------------------------


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
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
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
    assert copilot_log._unique_identifier("day_id", used) == "day_id"
    assert copilot_log._unique_identifier("day_id", used) == "day_id-2"
    assert copilot_log._unique_identifier("day_id", used) == "day_id-3"


# -- session extraction integration ------------------------------------------


def test_copilot_session_prefers_events_log_tools_over_db_files(tmp_path):
    session_id = NATIVE_IDS["tool_success"]
    db_path = _native_db_copy(
        tmp_path,
        "INSERT INTO session_files (session_id, file_path, tool_name, turn_index) VALUES (?, ?, ?, ?)",
        (session_id, "/tmp/from_db.py", "edit", 0),
    )
    _install_native_events("tool_success")
    with get_db_connection(db_path) as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, session_id)
        )
    calls = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
    assert [c["name"] for c in calls] == ["bash"]
    assert calls[0]["input"]["command"] == "printf TOOL_FIXTURE_MARKER"


# -- _extract_all --------------------------------------------------------------


def test_copilot_extract_all_reports_error_when_no_sessions(tmp_path, capsys):
    db_path = _native_db_copy(tmp_path, "DELETE FROM sessions")
    with get_db_connection(db_path) as conn:
        rc = copilot_log._extract_all(conn, None)
    assert rc == 1
    assert "no Copilot sessions" in capsys.readouterr().err


def test_copilot_extract_all_writes_one_file_per_session(tmp_path, monkeypatch, capsys):
    for case in NATIVE_IDS:
        _install_native_events(case)
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "output"
    with get_db_connection(db_path) as conn:
        assert copilot_log._extract_all(conn, str(output)) == 0
    written = list(output.iterdir())
    assert len(written) == len(NATIVE_IDS)
    assert all(p.suffix == ".json" for p in written)
    outputs = {
        case: json.loads(next(output.glob(f"*_{sid[:8]}.json")).read_text())
        for case, sid in NATIVE_IDS.items()
    }
    assert "BASELINE_FIXTURE_MARKER" in json.dumps(outputs["baseline"])
    assert "BLUE-WREN-17" in json.dumps(outputs["continued"])
    assert "TOOL_FIXTURE_MARKER" in json.dumps(outputs["tool_success"])
    assert "exit code 1" in json.dumps(outputs["tool_failure"])
    assert "SKILL_FIXTURE_MARKER" in json.dumps(outputs["skill"])
    user = next(e for e in outputs["image"]["events"] if e["role"] == "user")
    assert base64.b64decode(user["images"][0]["data"]) == PNG
    assert (
        f"done: {len(NATIVE_IDS)}/{len(NATIVE_IDS)} session(s) written"
        in capsys.readouterr().err
    )


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
    assert captured.err.count("skip") == len(NATIVE_IDS)
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
    rc = copilot_log.main(["bogus-id", "--db", str(FIXTURES / "session-store.db")])
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
    assert (
        copilot_log.main(
            [NATIVE_IDS["baseline"], "--db", str(FIXTURES / "session-store.db")],
            project_root=tmp_path if explicit_root else None,
        )
        == 0
    )
    [output] = tmp_path.iterdir()
    assert output.name.endswith(f"_{NATIVE_IDS['baseline'][:8]}.json")
    assert "BASELINE_FIXTURE_MARKER" in output.read_text()


def test_copilot_main_with_explicit_session_id_and_output_path_writes_there(tmp_path):
    out_path = tmp_path / "custom.json"
    rc = copilot_log.main(
        [
            NATIVE_IDS["baseline"],
            "--output",
            str(out_path),
            "--db",
            str(FIXTURES / "session-store.db"),
        ]
    )
    assert rc == 0
    envelope = json.loads(out_path.read_text())
    assert "BASELINE_FIXTURE_MARKER" in json.dumps(envelope)
    assert envelope["models"] == ["claude-sonnet-5"]


def test_copilot_main_reports_output_write_error(tmp_path, capsys):
    output = tmp_path / "directory.json"
    output.mkdir()
    assert (
        copilot_log.main(
            [
                NATIVE_IDS["baseline"],
                "--db",
                str(FIXTURES / "session-store.db"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("selector", [True, False])
def test_copilot_cli_stdout_is_json(selector, capsys):
    _install_native_events("tool_success")
    args = ["--db", str(FIXTURES / "session-store.db"), "--output", "-"]
    if selector:
        args.insert(0, NATIVE_IDS["tool_success"])
    assert copilot_log.main(args) == 0
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
    _install_native_events("tool_success")
    output = tmp_path / "session.json"
    assert (
        copilot_log.main(
            [
                NATIVE_IDS["tool_success"],
                "--db",
                str(FIXTURES / "session-store.db"),
                "--output",
                str(output),
                "--tool-result-bytes",
                "4",
            ]
        )
        == 0
    )
    envelope = json.loads(output.read_text())
    call = next(c for e in envelope["events"] for c in e.get("tool_calls", []))
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
    assert copilot_log.main(args, project_root=tmp_path / "output") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no exportable events" in captured.err
    assert not list(tmp_path.rglob("*.json"))


def test_copilot_db_file_fallback_preserves_names_and_targets(tmp_path):
    session_id = NATIVE_IDS["baseline"]
    target = " /tmp/a -> b.py "
    db_path = _native_db_copy(
        tmp_path,
        "INSERT INTO session_files (session_id, file_path, tool_name, turn_index) VALUES (?, ?, ?, ?)",
        (session_id, target, "edit", 0),
    )
    with get_db_connection(db_path) as conn:
        session = get_session_by_id(conn, session_id)
        envelope = copilot_log.extract_session(conn, session)
    [call] = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
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
    assert (
        copilot_log.main(
            [
                NATIVE_IDS["baseline"],
                "--db",
                str(FIXTURES / "session-store.db"),
                "--output",
                output,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "--tool-result-bytes" in captured.err
    if destination == "stdout":
        assert json.loads(captured.out)["harness"] == "copilot"
    else:
        assert captured.out == ""


@pytest.mark.parametrize("cap", [0, -1])
def test_copilot_tool_result_cap_boundaries(cap):
    _install_native_events("tool_success")
    with _native_db() as conn:
        envelope = copilot_log.extract_session(
            conn, get_session_by_id(conn, NATIVE_IDS["tool_success"]), cap
        )
    [call] = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
    assert envelope["truncation"]["tool_result_max_bytes"] == cap
    assert call["result_truncated"] is (cap == 0)
    assert call["result_bytes"] > 0
    if cap == 0:
        assert call["result"] == ""
    else:
        assert "TOOL_FIXTURE_MARKER" in call["result"]


@pytest.mark.parametrize("explicit_root", [False, True])
def test_copilot_all_default_destination_and_colliding_names(
    tmp_path, monkeypatch, explicit_root
):
    db_path = _native_db_copy(tmp_path, "UPDATE sessions SET cwd=?", (str(tmp_path),))
    output = tmp_path / "output" if explicit_root else tmp_path
    monkeypatch.setattr(copilot_log, "output_identifier", lambda session: "same")
    monkeypatch.chdir(tmp_path)
    assert (
        copilot_log.main(
            ["--db", str(db_path)], project_root=output if explicit_root else None
        )
        == 0
    )
    paths = sorted(output.glob("*.json"))
    assert len(paths) == len(NATIVE_IDS)
    assert (output / "copilot_session_log_raw_same.json").is_file()
    assert (output / "copilot_session_log_raw_same-2.json").is_file()
    assert all(json.loads(p.read_text())["events"] for p in paths)


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
        assert copilot_log.main(["selected"]) == 0
    conn.close.assert_called_once_with()


@pytest.mark.parametrize("session_id", [None, 123])
def test_output_identifier_rejects_non_string_ids(session_id):
    with pytest.raises(TypeError, match="session id must be a string"):
        copilot_log.output_identifier({"id": session_id})


def _supplied_native_source(case):
    with _native_db() as conn:
        metadata = get_session_by_id(conn, NATIVE_IDS[case])
        source = sources.read_source(conn, metadata)
    source.state_records = sources.parse_state_records(_native_lines(case))
    return source


def test_native_messages_keep_order_identity_and_complete_results():
    source = _supplied_native_source("tool_success")
    # Enlarge the captured result, retaining the native call/message links.
    complete = next(
        r for r in source.state_records if r["type"] == "tool.execution_complete"
    )
    complete["data"]["result"]["content"] = "é" * 20000
    complete["data"]["result"]["additional"] = {"recorded": True}
    session = copilot_log.interpret_source(source)
    events = session["events"]
    assert [e["role"] for e in events] == ["user", "assistant", "assistant"]
    native = [
        r
        for r in source.state_records
        if r["type"] in ("user.message", "assistant.message")
    ]
    assert [e["id"] for e in events] == [r["id"] for r in native]
    [call] = [c for e in events for c in e.get("tool_calls", [])]
    assert call["id"] == complete["data"]["toolCallId"]
    assert call["result"] == "é" * 20000
    assert call["result_record"]["content"] == complete["data"]["result"]
    assert "result_truncated" not in call


def test_native_repeated_or_missing_results_are_not_lost():
    source = _supplied_native_source("tool_success")
    complete = next(
        r for r in source.state_records if r["type"] == "tool.execution_complete"
    )
    # Duplicate completion is retained as unmatched; it must not overwrite the first.
    repeated = json.loads(json.dumps(complete))
    repeated["id"] = "repeat"
    repeated["data"]["result"] = {"content": "second result"}
    source.state_records.append(repeated)
    session = copilot_log.interpret_source(source)
    [call] = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert "TOOL_FIXTURE_MARKER" in call["result"]
    assert session["unmatched_tool_results"][0]["id"] == "repeat"
    source.state_records = [
        r for r in source.state_records if r["type"] != "tool.execution_complete"
    ]
    session = copilot_log.interpret_source(source)
    [call] = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert call["result"] is None
    assert "result_record" not in call


def test_native_image_ownership_uses_its_record_despite_timestamp():
    source = _supplied_native_source("image")
    # Misleading timestamps must not transfer the image to a later human message.
    user = next(r for r in source.state_records if r["type"] == "user.message")
    user["timestamp"] = "2099-01-01T00:00:00Z"
    later = json.loads(json.dumps(user))
    later["id"] = "later-user"
    later["data"]["messageId"] = "later-message"
    later["data"].pop("attachments")
    source.state_records.append(later)
    session = copilot_log.interpret_source(source)
    users = [e for e in session["events"] if e["role"] == "user"]
    assert len(users) == 2
    assert base64.b64decode(users[0]["images"][0]["data"]) == PNG
    assert not users[1].get("images")


@pytest.mark.parametrize(
    "result", [None, "", {}, {"content": "", "additional": [1, False]}, [1, 2]]
)
def test_native_empty_and_structured_results_survive(result):
    source = _supplied_native_source("tool_success")
    complete = next(
        r for r in source.state_records if r["type"] == "tool.execution_complete"
    )
    complete["data"]["result"] = result
    complete["data"]["success"] = False
    session = copilot_log.interpret_source(source)
    [call] = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert call["result_record"]["content"] == result
    assert call["is_error"] is True
    assert call["result"] == copilot_log._result_text(result)
    assert call["result_record"]["is_error"] is True


def test_native_tool_results_match_ids_instead_of_completion_order():
    source = _supplied_native_source("tool_success")
    request = next(r for r in source.state_records if r["type"] == "assistant.message")
    start = next(r for r in source.state_records if r["type"] == "tool.execution_start")
    complete = next(
        r for r in source.state_records if r["type"] == "tool.execution_complete"
    )
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
    calls = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert [c["id"] for c in calls] == [complete["data"]["toolCallId"], "other"]
    assert "TOOL_FIXTURE_MARKER" in calls[0]["result"]
    assert calls[0]["execution"]["input"]["extra"] == ["execution-only"]
    assert calls[0]["execution"]["id"] == start["id"]
    assert calls[1]["result"] == "other output"
    assert calls[1]["input"] == {"nested": [1, {"keep": True}]}
    assert session["unsupported_tools"] == ["unknown"]


@pytest.mark.parametrize("parent", [True, False])
def test_native_start_without_request_uses_only_recorded_parent(parent):
    source = _supplied_native_source("tool_success")
    request = next(r for r in source.state_records if r["type"] == "assistant.message")
    request["data"]["toolRequests"] = []
    if not parent:
        start = next(
            r for r in source.state_records if r["type"] == "tool.execution_start"
        )
        start["parentId"] = "unrecorded-message"
    session = copilot_log.interpret_source(source)
    tool_message = next(e for e in session["events"] if e.get("tool_calls"))
    assert (tool_message["id"] == request["id"]) is parent
    assert "TOOL_FIXTURE_MARKER" in tool_message["tool_calls"][0]["result"]


def test_partial_state_keeps_database_prose_and_unowned_activity():
    source = _supplied_native_source("tool_success")
    source.state_records = [
        r for r in source.state_records if r["type"].startswith("tool.")
    ]
    source.files = [{"file_path": "/db.py", "tool_name": "edit", "turn_index": 0}]
    session = copilot_log.interpret_source(source)
    assert any("Report its output" in e.get("text", "") for e in session["events"])
    calls = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert [c["name"] for c in calls] == ["bash"]
    source.state_records = [
        r for r in source.state_records if r["type"] == "tool.execution_complete"
    ]
    session = copilot_log.interpret_source(source)
    assert session["unmatched_tool_results"][0]["tool_call_id"]
    assert [c["name"] for e in session["events"] for c in e.get("tool_calls", [])] == [
        "edit"
    ]


def test_native_messages_keep_database_file_fallback_without_tool_activity():
    source = _supplied_native_source("baseline")
    source.files = [{"file_path": " a -> b.py ", "tool_name": "edit", "turn_index": 0}]
    session = copilot_log.interpret_source(source)
    [call] = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert call["input"] == {"target": " a -> b.py "}


def test_session_retains_skills_and_unassociated_permission_lifecycle():
    source = _supplied_native_source("skill")
    source.state_records.extend(
        sources.parse_state_records(_hand_lines("permissions.jsonl"))
    )
    session = copilot_log.interpret_source(source)
    assert session["skills_used"] == ["fixture-skill"]
    assert any("SKILL_FIXTURE_MARKER" in e.get("text", "") for e in session["events"])
    assert [p["decision"] for p in session["permission_decisions"]] == [
        "approved",
        "denied-interactively-by-user",
        "pending",
    ]
    assert session["permission_decisions"][1]["feedback"] == "not like that"


def test_owned_images_keep_distinct_paths_and_deduplicate_tokens():
    source = _supplied_native_source("image")
    user = next(r for r in source.state_records if r["type"] == "user.message")
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
    [message] = [e for e in session["events"] if e["role"] == "user"]
    assert len(message["images"]) == 2
    assert all(base64.b64decode(image["data"]) == PNG for image in message["images"])
    assert user["data"]["content"] == message["text"]


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
    assert copilot_log._json_value({"key": [True, None]}) == {"key": [True, None]}
    with pytest.raises(TypeError, match="JSON values"):
        copilot_log._json_value({1, 2})


def test_malformed_native_tools_do_not_invent_calls():
    source = _supplied_native_source("baseline")
    source.state_records.extend(
        [
            {"type": "tool.execution_start", "data": {}},
            {"type": "assistant.message", "data": {"toolRequests": [False]}},
        ]
    )
    session = copilot_log.interpret_source(source)
    assert not any(e.get("tool_calls") for e in session["events"])


def test_native_tools_preserve_recorded_database_file_activity():
    source = _supplied_native_source("tool_success")
    source.files = [
        {
            "file_path": "/tmp/file.py",
            "tool_name": "view",
            "turn_index": 0,
            "first_seen_at": "recorded time",
        }
    ]
    session = copilot_log.interpret_source(source)
    assert session["file_activity"] == source.files
    assert [c["name"] for e in session["events"] for c in e.get("tool_calls", [])] == [
        "bash"
    ]
