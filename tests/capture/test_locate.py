import json
import sqlite3

import pytest

from session_log_extractors.capture import locate


def jl(*entries):
    return "\n".join(json.dumps(e) for e in entries) + "\n"


@pytest.fixture
def root(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    return clone


def claude_lines(text="hi"):
    return jl(
        {
            "type": "user",
            "version": "2.1.5",
            "timestamp": "t",
            "message": {"role": "user", "content": text},
        },
        {"type": "attachment", "attachment": "dropped"},
    )


def test_inside_accepts_the_root_and_children_only(tmp_path, root):
    assert locate.inside(root, root)
    assert locate.inside(root, root / "src" / "app")
    assert not locate.inside(root, tmp_path / "elsewhere")
    assert not locate.inside(root, root.parent)
    assert not locate.inside(root, None)
    assert not locate.inside(root, "")


def test_claude_snapshot_reads_transcript_and_subagents(tmp_path, root):
    transcript = tmp_path / "abc.jsonl"
    transcript.write_text(claude_lines("main"))
    agents = tmp_path / "abc" / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-1.jsonl").write_text(claude_lines("sub"))
    (agents / "notes.txt").write_text("ignored")
    payload = {
        "session_id": "abc",
        "transcript_path": str(transcript),
        "cwd": str(root),
        "hook_event_name": "Stop",
    }

    snap = locate.claude_snapshot(payload, root)

    assert snap.harness == "claude"
    assert set(snap.files) == {
        "session_logs/claude/abc.jsonl",
        "session_logs/claude/abc/subagents/agent-1.jsonl",
    }
    assert "attachment" not in snap.files["session_logs/claude/abc.jsonl"]
    assert snap.meta == {
        "transcript_path_reported": str(transcript),
        "hook_event": "Stop",
        "harness_version": "2.1.5",
    }


def test_claude_snapshot_skips_sessions_outside_the_clone(tmp_path, root):
    transcript = tmp_path / "abc.jsonl"
    transcript.write_text(claude_lines())
    payload = {
        "session_id": "abc",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path / "other"),
    }

    assert locate.claude_snapshot(payload, root) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"transcript_path": "/x"},
        {"session_id": "../evil", "transcript_path": "/x"},
        {"session_id": "abc"},
        {"session_id": "abc", "transcript_path": "/does/not/exist.jsonl", "cwd": "."},
    ],
)
def test_claude_snapshot_needs_a_safe_id_and_readable_transcript(payload, root):
    assert locate.claude_snapshot(payload, root) is None


def codex_rollout(sid="s1"):
    return jl(
        {
            "type": "session_meta",
            "payload": {"id": sid, "cwd": "/w", "cli_version": "0.9"},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
    )


def test_codex_snapshot_finds_rollout_by_session_id(tmp_path, root):
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "20"
    day.mkdir(parents=True)
    (day / "rollout-2026-10-20T10-00-00-s1.jsonl").write_text(codex_rollout())
    payload = {"session_id": "s1", "cwd": str(root)}

    snap = locate.codex_snapshot(payload, root, tmp_path)

    assert snap.harness == "codex"
    assert set(snap.files) == {"session_logs/codex/s1.jsonl"}
    assert snap.meta["harness_version"] == "0.9"


def test_codex_snapshot_prefers_reported_transcript_and_reads_id_from_meta(
    tmp_path, root
):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text(codex_rollout("from-meta"))
    payload = {"transcript_path": str(rollout), "cwd": str(root)}

    snap = locate.codex_snapshot(payload, root, tmp_path)

    assert snap.session_id == "from-meta"


def test_codex_snapshot_honours_codex_home(tmp_path, root, monkeypatch):
    home = tmp_path / "custom"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "rollout-x-s2.jsonl").write_text(codex_rollout("s2"))
    monkeypatch.setenv("CODEX_HOME", str(home))

    snap = locate.codex_snapshot({"session_id": "s2", "cwd": str(root)}, root, tmp_path)

    assert snap.session_id == "s2"


def test_codex_snapshot_returns_none_without_a_rollout_or_id(tmp_path, root):
    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    bare = tmp_path / "bare.jsonl"
    bare.write_text(jl({"type": "event_msg", "payload": {"type": "agent_message"}}))

    assert (
        locate.codex_snapshot({"session_id": "nope", "cwd": str(root)}, root, tmp_path)
        is None
    )
    assert (
        locate.codex_snapshot(
            {"transcript_path": str(bare), "cwd": str(root)}, root, tmp_path
        )
        is None
    )
    assert (
        locate.codex_snapshot({"session_id": "s", "cwd": "/elsewhere"}, root, tmp_path)
        is None
    )


def make_copilot_home(tmp_path, sid="c1", with_db=True):
    state = tmp_path / ".copilot" / "session-state" / sid
    state.mkdir(parents=True)
    events = jl(
        {
            "type": "session.start",
            "data": {
                "sessionId": sid,
                "copilotVersion": "1.0.90",
                "context": {"cwd": "/w"},
            },
        },
        {
            "type": "user.message",
            "data": {"content": "hello", "transformedContent": "reminder"},
        },
    )
    (state / "events.jsonl").write_text(events)
    if with_db:
        conn = sqlite3.connect(tmp_path / ".copilot" / "session-store.db")
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, branch TEXT, summary TEXT,
                                   created_at TEXT, updated_at TEXT, secret_col TEXT);
            CREATE TABLE turns (session_id TEXT, turn_index INT, user_message TEXT,
                                assistant_response TEXT, timestamp TEXT);
            CREATE TABLE attachments (session_id TEXT, display_name TEXT, path TEXT, type TEXT);
            CREATE TABLE session_refs (other TEXT);
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (sid, "/w", "r", "b", "s", "c", "u", "x"),
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            ("other", "/w", "r", "b", "s", "c", "u", "x"),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?)",
            (sid, 0, "go\nPASSWORD=abcdefgh1", "ok", "t"),
        )
        conn.execute(
            "INSERT INTO attachments VALUES (?,?,?,?)",
            (sid, "a.png", "/w/a.png", "image"),
        )
        conn.commit()
        conn.close()


def test_copilot_snapshot_collects_events_and_a_db_dump(tmp_path, root):
    make_copilot_home(tmp_path)
    payload = {
        "sessionId": "c1",
        "cwd": str(root),
        "transcriptPath": "/somewhere/else.jsonl",
    }

    snap = locate.copilot_snapshot(payload, root, tmp_path)

    assert set(snap.files) == {
        "session_logs/copilot/c1/events.jsonl",
        "session_logs/copilot/c1/copilot_db.json",
    }
    assert (
        "transformedContent" not in snap.files["session_logs/copilot/c1/events.jsonl"]
    )
    dump = json.loads(snap.files["session_logs/copilot/c1/copilot_db.json"])
    assert [s["id"] for s in dump["sessions"]] == ["c1"]
    assert "secret_col" not in dump["sessions"][0]
    assert dump["turns"][0]["user_message"] == "go\n[line removed: possible secret]"
    assert dump["attachments"] == [
        {
            "session_id": "c1",
            "display_name": "a.png",
            "path": "/w/a.png",
            "type": "image",
        }
    ]
    assert snap.meta["transcript_path_reported"] == "/somewhere/else.jsonl"
    assert snap.meta["harness_version"] == "1.0.90"


def test_copilot_snapshot_works_without_a_database(tmp_path, root):
    make_copilot_home(tmp_path, with_db=False)

    snap = locate.copilot_snapshot(
        {"sessionId": "c1", "cwd": str(root)}, root, tmp_path
    )

    assert set(snap.files) == {"session_logs/copilot/c1/events.jsonl"}


def test_copilot_snapshot_tolerates_a_broken_database(tmp_path, root):
    make_copilot_home(tmp_path, with_db=False)
    (tmp_path / ".copilot" / "session-store.db").write_text("not sqlite")

    snap = locate.copilot_snapshot(
        {"sessionId": "c1", "cwd": str(root)}, root, tmp_path
    )

    assert set(snap.files) == {"session_logs/copilot/c1/events.jsonl"}


def test_copilot_snapshot_honours_copilot_home_and_skips_foreign_sessions(
    tmp_path, root, monkeypatch
):
    make_copilot_home(tmp_path)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "missing"))

    assert (
        locate.copilot_snapshot({"sessionId": "c1", "cwd": str(root)}, root, tmp_path)
        is None
    )
    monkeypatch.delenv("COPILOT_HOME")
    assert (
        locate.copilot_snapshot(
            {"sessionId": "c1", "cwd": "/elsewhere"}, root, tmp_path
        )
        is None
    )
    assert locate.copilot_snapshot({"cwd": str(root)}, root, tmp_path) is None


def test_copilot_db_dump_ignores_tables_missing_expected_columns(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE turns (unrelated TEXT)")
    conn.execute("CREATE TABLE checkpoints (session_id TEXT, title TEXT)")
    conn.execute("INSERT INTO checkpoints VALUES ('c1', 't')")
    conn.commit()
    conn.close()

    dump = locate.copilot_db_dump(db, "c1")

    assert dump["turns"] == []
    assert dump["checkpoints"] == [{"session_id": "c1", "title": "t"}]
    assert locate.copilot_db_dump(tmp_path / "absent.db", "c1") == {}


def test_detect_harness_from_payload_shape():
    assert locate.detect_harness({"sessionId": "x"}) == "copilot"
    assert (
        locate.detect_harness({"transcript_path": "/home/u/.codex/sessions/r.jsonl"})
        == "codex"
    )
    assert (
        locate.detect_harness({"transcript_path": "/home/u/.claude/projects/p/s.jsonl"})
        == "claude"
    )
    assert locate.detect_harness({"cwd": "/w"}) is None


def test_build_snapshot_dispatches_and_rejects_unknown_harnesses(tmp_path, root):
    transcript = tmp_path / "abc.jsonl"
    transcript.write_text(claude_lines())
    payload = {
        "session_id": "abc",
        "transcript_path": str(transcript),
        "cwd": str(root),
    }

    assert locate.build_snapshot("claude", payload, root, tmp_path).session_id == "abc"
    assert locate.build_snapshot("codex", payload, root, tmp_path).harness == "codex"
    assert locate.build_snapshot("copilot", {"cwd": str(root)}, root, tmp_path) is None
    assert locate.build_snapshot("mystery", payload, root, tmp_path) is None


def test_copilot_db_dump_skips_tables_without_a_session_key(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE turns (user_message TEXT)")
    conn.execute("INSERT INTO turns VALUES ('x')")
    conn.commit()
    conn.close()

    assert locate.copilot_db_dump(db, "c1")["turns"] == []


def test_copilot_snapshot_without_a_version_event_reports_none(tmp_path, root):
    state = tmp_path / ".copilot" / "session-state" / "c2"
    state.mkdir(parents=True)
    (state / "events.jsonl").write_text(
        jl({"type": "user.message", "data": {"content": "hi"}})
    )

    snap = locate.copilot_snapshot(
        {"sessionId": "c2", "cwd": str(root)}, root, tmp_path
    )

    assert snap.meta["harness_version"] is None
