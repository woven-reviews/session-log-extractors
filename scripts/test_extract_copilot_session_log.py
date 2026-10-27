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

from session_log_extractors import extract_copilot_session_log as copilot_log
from session_log_extractors.extract_copilot_session_log import (
    Turn,
    _permissions_from_events,
    _skills_from_events,
    attribute_permissions,
    attribute_skills,
    build_events,
    clean_user_text,
    get_db_connection,
    get_session_by_id,
    get_session_turns,
    matching_sessions,
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
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", tmp_path / "session-state")


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
    session_dir = copilot_log.COPILOT_STATE_ROOT / session_id
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


def test_copilot_skill_invocation_is_recorded_and_attributed():
    skills = _skills_from_events(_native_lines("skill"))
    assert len(skills) == 1
    assert skills[0]["name"] == "fixture-skill"
    assert skills[0]["path"] == "/workspace/.github/skills/fixture-skill/SKILL.md"
    turn = Turn("use skill", "", "2026-01-01T00:00:00Z", 0)
    attribute_skills([turn], skills)
    assert turn.skills_used == ["fixture-skill"]


def test_copilot_permissions_join_approval_denial_and_pending():
    perms = _permissions_from_events(_hand_lines("permissions.jsonl"))
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


def test_copilot_attribute_permissions_by_timestamp():
    t0 = Turn("first", "", "2026-07-01T22:00:00Z", 0)
    t1 = Turn("second", "", "2026-07-01T22:10:00Z", 1)
    perms = [
        {
            "tool": "a",
            "decision": "approved",
            "feedback": "",
            "timestamp": "2026-07-01T21:55:00Z",
        },
        {
            "tool": "b",
            "decision": "denied",
            "feedback": "no",
            "timestamp": "2026-07-01T22:05:00Z",
        },
    ]
    attribute_permissions([t0, t1], perms)
    assert [p["tool"] for p in t0.permission_decisions] == ["a"]
    assert [p["tool"] for p in t1.permission_decisions] == ["b"]


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


def test_copilot_prefix_matching():
    session_id = NATIVE_IDS["baseline"]
    with _native_db() as conn:
        assert get_session_by_id(conn, session_id)["id"] == session_id
        assert get_session_by_id(conn, session_id[:8])["id"] == session_id


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


def _copilot_turn_with_image(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    turn = Turn(
        "look at [image: a.png] and fix it",
        "Sure, fixing.",
        "2026-08-01T10:00:00Z",
        0,
    )
    turn.image_refs = [
        {"name": "a.png", "path": str(img), "type": "image/png"},
        {"name": "ghost.png"},
    ]
    turn.add_tool("view", "backend/app/models.py")
    return turn


def test_build_events_inlines_an_attachment_resolved_from_disk(tmp_path):
    user = build_events([_copilot_turn_with_image(tmp_path)])[0]
    assert base64.b64decode(user["images"][0]["data"]) == PNG


def test_build_events_records_an_inline_token_with_no_attachment(tmp_path):
    # A [image: name] token with no matching attachment record is common in
    # Copilot logs; it has to stay visible.
    user = build_events([_copilot_turn_with_image(tmp_path)])[0]
    assert user["images"][1] == {"unavailable": True, "ref": "ghost.png"}


def test_build_events_recovers_tool_name_and_target(tmp_path):
    events = build_events([_copilot_turn_with_image(tmp_path)])
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "view"
    assert call["input"] == {"target": "backend/app/models.py"}


def test_build_events_preserves_user_text_and_image_references(tmp_path):
    turn = _copilot_turn_with_image(tmp_path)
    original_text = turn.user_text
    original_refs = [dict(ref) for ref in turn.image_refs]
    assert build_events([turn]) == build_events([turn])
    assert turn.user_text == original_text
    assert turn.image_refs == original_refs


def test_build_events_emits_an_assistant_event_for_a_reply(tmp_path):
    events = build_events([_copilot_turn_with_image(tmp_path)])
    assistant = [e for e in events if e["role"] == "assistant"]
    assert assistant and assistant[0]["text"] == "Sure, fixing."


def test_copilot_tools_from_events_join_start_and_complete():
    calls = copilot_log._tools_from_events(_native_lines("tool_success"))
    assert len(calls) == 1
    assert calls[0]["name"] == "bash"
    assert calls[0]["arguments"]["command"] == "printf TOOL_FIXTURE_MARKER"
    assert calls[0]["success"] is True
    assert "TOOL_FIXTURE_MARKER" in calls[0]["result"]


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
    assert copilot_log.get_state_tools(NATIVE_IDS["tool_failure"])[0]["success"] is True
    calls = [c for e in envelope["events"] for c in e.get("tool_calls", [])]
    assert len(calls) == 1
    assert calls[0]["input"]["command"] == "false"
    assert "exit code 1" in calls[0]["result"]


def test_copilot_native_skill_is_retained_on_its_turn():
    _install_native_events("skill")
    with _native_db() as conn:
        turns = copilot_log.load_session_turns(
            conn, get_session_by_id(conn, NATIVE_IDS["skill"])
        )
    assert turns[0].skills_used == ["fixture-skill"]
    assert "SKILL_FIXTURE_MARKER" in turns[0].assistant_text


def test_copilot_attribute_tools_populates_target_note_and_envelope():
    turn = Turn("find audits", "", "2026-07-01T22:00:00Z", 0)
    tools = [
        {
            "name": "grep",
            "arguments": {"pattern": "audit"},
            "success": True,
            "result": "backend/app/models.py",
            "timestamp": "2026-07-01T22:00:01Z",
        }
    ]
    copilot_log.attribute_tools([turn], tools)
    assert turn.tools == [("grep", "audit")]
    assert turn.result_notes == ["grep: backend/app/models.py"]
    # Structured call is carried into the raw envelope with its result.
    call = next(c for e in build_events([turn]) for c in e.get("tool_calls", []))
    assert call["name"] == "grep"
    assert call["input"] == {"pattern": "audit"}
    assert call["result"] == "backend/app/models.py"


def test_copilot_attribute_tools_flags_failure():
    turn = Turn("run it", "", "2026-07-01T22:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn],
        [
            {
                "name": "bash",
                "arguments": {"command": "false"},
                "success": False,
                "result": "boom",
                "timestamp": "2026-07-01T22:00:01Z",
            }
        ],
    )
    assert turn.result_notes == ["bash: error: boom"]


def test_copilot_attribute_tools_by_timestamp():
    t0 = Turn("first", "", "2026-07-01T22:00:00Z", 0)
    t1 = Turn("second", "", "2026-07-01T22:10:00Z", 1)
    tools = [
        {
            "name": "view",
            "arguments": {"path": "a.py"},
            "success": True,
            "result": "ok",
            "timestamp": "2026-07-01T22:12:00Z",
        }
    ]
    copilot_log.attribute_tools([t0, t1], tools)
    assert t0.tools == []
    assert t1.tools == [("view", "a.py")]


def test_copilot_attribute_tools_uses_turn_completion_time():
    # DB turn timestamps mark completion (see the continued fixture), so a call
    # between two completions belongs to the later turn.
    t0 = Turn("first", "", "2026-01-01T00:00:05Z", 0)
    t1 = Turn("second", "", "2026-01-01T00:00:15Z", 1)
    copilot_log.attribute_tools(
        [t0, t1],
        [
            {
                "name": "bash",
                "arguments": {"command": "ls"},
                "timestamp": "2026-01-01T00:00:10Z",
            }
        ],
    )
    assert [c["name"] for c in t0.tool_calls] == []
    assert [c["name"] for c in t1.tool_calls] == ["bash"]


def test_copilot_build_events_emits_one_timestamped_event_per_tool_call():
    turn = Turn("do two things", "On it.", "2026-07-01T22:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn],
        [
            {
                "name": "grep",
                "arguments": {"pattern": "a"},
                "success": True,
                "result": "r1",
                "timestamp": "2026-07-01T22:00:01Z",
            },
            {
                "name": "view",
                "arguments": {"path": "b.py"},
                "success": True,
                "result": "r2",
                "timestamp": "2026-07-01T22:00:02Z",
            },
        ],
    )
    events = build_events([turn])
    # user + assistant prose + one event per tool call.
    assert [e["role"] for e in events] == [
        "user",
        "assistant",
        "assistant",
        "assistant",
    ]
    tool_events = [e for e in events if e.get("tool_calls")]
    assert len(tool_events) == 2
    # Each tool event carries exactly one call stamped with that call's own time.
    assert tool_events[0]["ts"] == "2026-07-01T22:00:01Z"
    assert tool_events[0]["tool_calls"][0]["name"] == "grep"
    assert tool_events[1]["ts"] == "2026-07-01T22:00:02Z"
    assert tool_events[1]["tool_calls"][0]["name"] == "view"
    # Indices stay contiguous across the split-out events.
    assert [e["i"] for e in events] == [0, 1, 2, 3]


def test_copilot_main_defaults_to_all_without_a_selector(monkeypatch, tmp_path):
    called = {}

    def fake_extract_all(conn, output, strict, tool_result_max_bytes, *, project_root):
        called["output"] = output
        called["strict"] = strict
        called["project_root"] = project_root
        return 0

    monkeypatch.setattr(copilot_log, "_extract_all", fake_extract_all)
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    rc = copilot_log.main(["--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert called == {"output": None, "strict": False, "project_root": Path.cwd()}


# -- tool/result/skill descriptor edge cases -------------------------------


def test_decode_arguments_parses_json_string_and_falls_back_on_invalid():
    assert copilot_log._decode_arguments('{"a": 1}') == {"a": 1}
    assert copilot_log._decode_arguments("not json") == "not json"
    assert copilot_log._decode_arguments({"a": 1}) == {"a": 1}


def test_tool_descriptor_non_dict_and_json_fallback():
    assert copilot_log.tool_descriptor("echo", "just a string") == "just a string"
    assert copilot_log.tool_descriptor("echo", None) == ""
    # No known key present -> falls back to the full JSON blob.
    assert (
        copilot_log.tool_descriptor("mystery", {"other": "value"})
        == '{"other": "value"}'
    )
    # A value json.dumps cannot serialize falls back to "".
    assert copilot_log.tool_descriptor("mystery", {"bad": {1, 2}}) == ""


def test_result_note_handles_none_dict_and_unserializable():
    assert copilot_log.result_note(None) == ""
    assert copilot_log.result_note({"a": 1}) == '{"a": 1}'
    assert copilot_log.result_note({"bad": {1, 2}}) == ""


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


def test_copilot_turn_add_tool_without_descriptor():
    turn = Turn("x", "", "2026-01-01T00:00:00Z", 0)
    turn.add_tool("ls", "")
    assert turn.tools == [("ls", "")]


# -- DB/path helpers ---------------------------------------------------------


def test_copilot_get_db_connection_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        get_db_connection(Path("/nonexistent/copilot/session-store.db"))


def test_copilot_table_columns_without_row_factory():
    conn = sqlite3.connect(":memory:")  # no row_factory set -> plain tuples
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    assert copilot_log._table_columns(conn, "t") == {"id", "name"}


def test_copilot_paths_overlap_both_directions_and_neither(tmp_path):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    other = tmp_path / "c"
    other.mkdir()
    assert copilot_log._paths_overlap(tmp_path, child) is True
    assert copilot_log._paths_overlap(child, tmp_path) is True
    assert copilot_log._paths_overlap(child, other) is False


def test_copilot_cwd_matches_blank_and_strict(tmp_path):
    assert copilot_log._cwd_matches(None, Path.cwd()) is False
    assert copilot_log._cwd_matches("", Path.cwd()) is False
    assert copilot_log._cwd_matches(str(Path.cwd()), Path.cwd(), strict=True) is True
    assert (
        copilot_log._cwd_matches(str(tmp_path / "sub"), tmp_path, strict=True) is False
    )


def test_copilot_newest_session_falls_back_to_global_newest():
    # Native sessions ran in /workspace, which never overlaps the test cwd.
    with _native_db() as conn:
        session = copilot_log.newest_session(conn, Path.cwd(), strict=False)
    assert session["id"] == NATIVE_IDS["image"]


def test_copilot_newest_session_strict_with_no_match_returns_none():
    with _native_db() as conn:
        assert copilot_log.newest_session(conn, Path.cwd(), strict=True) is None


def test_copilot_newest_session_returns_none_for_empty_db(tmp_path):
    db_path = _native_db_copy(tmp_path, "DELETE FROM sessions")
    with get_db_connection(db_path) as conn:
        assert copilot_log.newest_session(conn, Path.cwd(), strict=False) is None


def test_copilot_newest_session_picks_most_recently_updated():
    # The image case was captured last, so it has the latest updated_at.
    with _native_db() as conn:
        session = copilot_log.newest_session(conn, Path("/workspace"), strict=True)
    assert session["id"] == NATIVE_IDS["image"]


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
    assert copilot_log.get_state_tools("sess-oserr") == []


def test_copilot_permissions_from_events_skips_malformed_lines():
    assert _permissions_from_events(_hand_lines("malformed.jsonl")) == []


def test_copilot_skills_from_events_skips_malformed_and_bad_data():
    assert _skills_from_events(_hand_lines("malformed.jsonl")) == []


def test_copilot_result_text_variants():
    assert copilot_log._result_text({"other": "x"}) == ""
    assert copilot_log._result_text("plain string") == "plain string"
    assert copilot_log._result_text(None) == ""
    assert copilot_log._result_text([1, 2, 3]) == "[1, 2, 3]"
    assert copilot_log._result_text({1, 2}) == ""


def test_copilot_tools_from_events_skips_malformed_lines_and_orphan_events():
    assert copilot_log._tools_from_events(_hand_lines("malformed.jsonl")) == []


def test_copilot_attribute_tools_falls_back_to_first_turn_without_timestamp():
    turn = Turn("do it", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn], [{"name": "grep", "arguments": None, "success": True, "result": "ok"}]
    )
    assert turn.tools == [("grep", "")]


def test_copilot_attribute_tools_with_no_turns_is_a_noop():
    copilot_log.attribute_tools(
        [],
        [{"name": "grep", "arguments": {}, "timestamp": "2026-01-01T00:00:00Z"}],
    )


def test_copilot_attribute_tools_registers_skill_from_call():
    tools = copilot_log._tools_from_events(_native_lines("skill"))
    assert [t["name"] for t in tools] == ["skill"]
    turn = Turn("use the skill", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_tools([turn], tools)
    assert turn.skills_used == ["fixture-skill"]


def test_copilot_attribute_skills_falls_back_to_first_turn_without_timestamp():
    turn = Turn("hi", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_skills([turn], [{"name": "demo"}])
    assert turn.skills_used == ["demo"]


def test_copilot_attribute_permissions_falls_back_to_first_turn_without_timestamp():
    turn = Turn("hi", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_permissions(
        [turn], [{"tool": "a", "decision": "approved", "feedback": ""}]
    )
    assert [p["tool"] for p in turn.permission_decisions] == ["a"]


def test_copilot_get_session_models_reads_native_usage_events():
    # The continued session logs one usage event per turn, both on one model.
    with _native_db() as conn:
        assert copilot_log.get_session_models(conn, NATIVE_IDS["continued"]) == [
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
    assert copilot_log.get_session_models(conn, "s") == ["gpt-4", "o3"]


def test_copilot_get_session_models_without_usage_table_is_empty():
    assert copilot_log.get_session_models(sqlite3.connect(":memory:"), "any") == []


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


def test_copilot_build_turn_objects_defaults_turn_index_and_parses_shell_command():
    raw_turns = [
        {
            "user_message": "<bash-input>pytest -q</bash-input>",
            "assistant_response": "ran it",
            "timestamp": "2026-01-01T00:00:00Z",
            # no turn_index key -> falls back to enumerate position
        }
    ]
    turns = copilot_log._build_turn_objects(raw_turns, [], [])
    assert turns[0].turn_index == 0
    assert turns[0].shell_command == "pytest -q"


def test_copilot_build_turn_objects_ignores_blank_image_token():
    raw_turns = [
        {
            "turn_index": 0,
            "user_message": "[image:   ] look",
            "assistant_response": "",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ]
    turns = copilot_log._build_turn_objects(raw_turns, [], [])
    assert turns[0].image_refs == []


def test_copilot_timestamped_attachment_belongs_to_second_turn():
    turns = copilot_log._build_turn_objects(
        [
            {"user_message": "first", "timestamp": "2026-01-01T00:00:05Z"},
            {"user_message": "second", "timestamp": "2026-01-01T00:00:15Z"},
        ],
        [],
        [
            {
                "display_name": "later.png",
                "path": "/tmp/later.png",
                "timestamp": "2026-01-01T00:00:10Z",
            },
            {
                "display_name": "notes.png",
                "path": "/tmp/notes.png",
                "type": "text/plain",
                "timestamp": "2026-01-01T00:00:10Z",
            },
        ],
    )
    assert turns[0].image_refs == []
    assert turns[1].image_refs == [{"name": "later.png", "path": "/tmp/later.png"}]


def test_copilot_attachment_with_zulu_timestamp_matches_naive_db_turns():
    turns = copilot_log._build_turn_objects(
        [{"user_message": "first", "timestamp": "2026-01-01 00:00:05"}],
        [],
        [
            {
                "display_name": "p.png",
                "path": "/tmp/p.png",
                "timestamp": "2026-01-01T00:00:01Z",
            }
        ],
    )
    assert turns[0].image_refs == [{"name": "p.png", "path": "/tmp/p.png"}]


def test_copilot_attachment_names_from_path_and_skips_non_images():
    turns = copilot_log._build_turn_objects(
        [{"user_message": "look", "timestamp": "2026-01-01T00:00:05Z"}],
        [],
        [
            {"path": "/tmp/shot.png", "timestamp": "2026-01-01T00:00:01Z"},
            {
                "display_name": "notes",
                "path": "/tmp/notes.txt",
                "timestamp": "2026-01-01T00:00:02Z",
            },
        ],
    )
    assert turns[0].image_refs == [{"name": "shot.png", "path": "/tmp/shot.png"}]


def test_copilot_attachments_sharing_a_display_name_are_both_kept():
    turns = copilot_log._build_turn_objects(
        # Native turn text carries no image token (see the image fixture).
        [{"user_message": "compare these", "timestamp": "2026-01-01T00:00:05Z"}],
        [],
        [
            {
                "display_name": "image.png",
                "path": "/tmp/a/image.png",
                "timestamp": "2026-01-01T00:00:01Z",
            },
            {
                "display_name": "image.png",
                "path": "/tmp/b/image.png",
                "timestamp": "2026-01-01T00:00:02Z",
            },
        ],
    )
    assert [r["path"] for r in turns[0].image_refs] == [
        "/tmp/a/image.png",
        "/tmp/b/image.png",
    ]


def test_copilot_timestamped_attachment_does_not_duplicate_image_token():
    turns = copilot_log._build_turn_objects(
        [{"user_message": "[image: pixel.png]", "timestamp": "2026-01-01T00:00:05Z"}],
        [],
        [
            {
                "display_name": "pixel.png",
                "path": "/tmp/pixel.png",
                "timestamp": "2026-01-01T00:00:01Z",
            }
        ],
    )
    assert turns[0].image_refs == [
        {"name": "pixel.png", "path": "/tmp/pixel.png", "type": ""}
    ]


def test_copilot_image_token_and_same_named_attachment_at_another_path_stay_distinct():
    turns = copilot_log._build_turn_objects(
        [{"user_message": "[image: p.png]", "timestamp": "2026-01-01T00:00:05Z"}],
        [],
        [
            {"display_name": "p.png", "path": "/a/p.png"},
            {
                "display_name": "p.png",
                "path": "/b/p.png",
                "timestamp": "2026-01-01T00:00:01Z",
            },
        ],
    )
    assert [r["path"] for r in turns[0].image_refs] == ["/a/p.png", "/b/p.png"]


# -- image byte resolution --------------------------


def test_copilot_image_bytes_and_ext_from_data_url():
    event = json.loads(_hand_lines("attachment_variants.jsonl")[1])
    data_url = event["data"]["attachments"][0]["dataUrl"]
    raw, ext = copilot_log._image_bytes_and_ext({"data_url": data_url})
    assert ext == "png"
    assert raw == PNG


def test_copilot_image_bytes_and_ext_bad_data_url_base64_returns_none():
    assert (
        copilot_log._image_bytes_and_ext({"data_url": "data:image/png;base64,A"})
        is None
    )


def test_copilot_image_bytes_and_ext_from_inline_data_field():
    event = json.loads(_hand_lines("attachment_variants.jsonl")[2])
    attachment = event["data"]["attachments"][0]
    raw, ext = copilot_log._image_bytes_and_ext(
        {"data": attachment["data"], "media_type": attachment["mediaType"]}
    )
    assert ext == "png"
    assert raw == PNG


def test_copilot_image_bytes_and_ext_bad_inline_data_returns_none():
    assert copilot_log._image_bytes_and_ext({"data": "A"}) is None


def test_copilot_image_bytes_and_ext_missing_path_returns_none():
    assert copilot_log._image_bytes_and_ext({"path": "/does/not/exist.png"}) is None


def test_copilot_image_bytes_and_ext_guesses_extension_from_type_when_no_suffix(
    tmp_path,
):
    img = tmp_path / "noext"
    img.write_bytes(PNG)
    raw, ext = copilot_log._image_bytes_and_ext({"path": str(img), "type": "image/png"})
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
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        copilot_log, "newest_session", lambda conn, cwd, strict=False: None
    )
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
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        copilot_log, "newest_session", lambda conn, cwd, strict=False: session
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


def test_copilot_session_loader_preserves_permission_decisions(tmp_path):
    _write_events_log(NATIVE_IDS["baseline"], _hand_lines("permissions.jsonl"))
    with _native_db() as conn:
        turns = copilot_log.load_session_turns(
            conn, get_session_by_id(conn, NATIVE_IDS["baseline"])
        )
    assert [p["decision"] for p in turns[0].permission_decisions] == [
        "approved",
        "denied-interactively-by-user",
        "pending",
    ]
    assert turns[0].permission_decisions[1]["feedback"] == "not like that"


def test_copilot_retained_database_metadata_readers(tmp_path):
    session_id = NATIVE_IDS["baseline"]
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
    with get_db_connection(db_path) as conn:
        assert copilot_log.get_session_refs(conn, session_id) == [
            {
                "ref_type": "commit",
                "ref_value": "abc123",
                "turn_index": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        assert copilot_log.get_session_checkpoints(conn, session_id) == [
            {
                "checkpoint_number": 1,
                "title": "Checkpoint one",
                "overview": "did stuff",
                "created_at": "2026-01-01T00:05:00Z",
            }
        ]


@pytest.mark.parametrize("fail", [False, True])
def test_main_closes_connection(monkeypatch, fail):
    conn = Mock()
    monkeypatch.setattr(copilot_log, "get_db_connection", lambda path: conn)
    extract = Mock(return_value=0, side_effect=ValueError("failure") if fail else None)
    monkeypatch.setattr(copilot_log, "_extract_selected", extract)
    if fail:
        with pytest.raises(ValueError, match="failure"):
            copilot_log.main(["selected"])
    else:
        assert copilot_log.main(["selected"]) == 0
    conn.close.assert_called_once_with()
