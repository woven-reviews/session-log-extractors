#!/usr/bin/env python3
"""Minimal asserts for extract_copilot_session_log.

Run: python3 scripts/test_extract_copilot_session_log.py
"""

from __future__ import annotations

import base64
import json
import re
import pytest
import sqlite3
import tempfile
from pathlib import Path

import extract_copilot_session_log as copilot_log
from extract_copilot_session_log import (
    Turn,
    build_events,
    attribute_permissions,
    attribute_skills,
    clean_user_text,
    format_decision,
    get_db_connection,
    get_session_by_id,
    get_session_turns,
    main,
    matching_sessions,
    newest_session,
    render_session,
)

# Real, trimmed, redacted Copilot session-store.db + events.jsonl sidecar --
# see tests/fixtures/copilot/.
_COPILOT_FIXTURES = Path(__file__).resolve().parent.parent / "tests/fixtures/copilot"
COPILOT_FIXTURE_DB = _COPILOT_FIXTURES / "session-store.db"
COPILOT_FIXTURE_STATE_ROOT = _COPILOT_FIXTURES / "session-state"
COPILOT_FIXTURE_SESSION_ID = "b10f9a1b-1803-4178-8fcc-8b2a15134624"
# Second real session: captures a skill invocation and a real permission
# denial (Copilot's headless fallback denying a read it couldn't ask a human
# about). See scripts/fixtures/generate_fixtures.sh.
COPILOT_FIXTURE_SKILL_SESSION_ID = "7fc0ab52-6795-49e8-8ab9-5048733e64bc"
# Third real session, captured after fixing the image-attachment path (it
# now lives inside the scratch repo, so it's no longer denied) and the
# --deny-tool pattern bugs (bare '*' isn't a wildcard; the model deletes via
# apply_patch's write() kind, not just shell()). This one real turn hit
# BOTH deny rules in a row -- apply_patch denied by `write`, then a bash
# `rm` denied by `shell(rm)` -- and the model gave up, so it's the richest
# real denial capture we have. Also carries a real session_refs commit row
# and (again) a real skill invocation.
COPILOT_FIXTURE_DENIAL_SESSION_ID = "5ff8ca94-46ab-42fb-b09b-a48620524ac4"

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/ax6fN8A"
    "AAAASUVORK5CYII="
)


def test_copilot_noise_cleaning():
    assert clean_user_text("<system-notification>test</system-notification>") == ""
    assert clean_user_text("<bash-stdout>output</bash-stdout>") == ""
    assert (
        clean_user_text(
            "Real text\n<system-reminder>ignore</system-reminder>\nMore text"
        )
        == "Real text\n\nMore text"
    )


def test_copilot_skill_definition_details_are_loaded():
    # load_skill_details always re-reads the SKILL.md off disk by path, and
    # the real fixture-skill's original path (under the now-deleted scratch
    # repo) no longer exists -- so this still needs a file on disk. But its
    # *content* is the real captured skill.invoked event's own fields, not a
    # fabricated one, matching the SKILL.md that scripts/fixtures/
    # generate_fixtures.sh actually seeded to produce that real capture.
    lines = (
        (COPILOT_FIXTURE_STATE_ROOT / COPILOT_FIXTURE_SKILL_SESSION_ID / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    skill = copilot_log._skills_from_events(lines)[0]
    raw_event = next(json.loads(line) for line in lines if '"skill.invoked"' in line)
    name = raw_event["data"]["name"]
    description = raw_event["data"]["description"]
    body = raw_event["data"]["content"]

    with tempfile.TemporaryDirectory() as tmp_name:
        skill_path = Path(tmp_name) / name / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
            encoding="utf-8",
        )
        turn = Turn("use the skill", "", "2026-07-01T22:00:00Z", 0)
        attribute_skills([turn], [{**skill, "path": str(skill_path)}])
        assert turn.skill_details[name]["description"] == description
        assert turn.skill_details[name]["path"] == str(skill_path.resolve())


def test_copilot_format_decision():
    assert format_decision("approved") == "approved"
    assert format_decision("approved-for-location") == "approved"
    assert format_decision("denied-interactively-by-user") == "denied"
    assert format_decision("") == "unknown"


def test_copilot_attribute_permissions_by_timestamp():
    # No real Copilot session captured a permission decision (see above) --
    # this fabricates the already-parsed permission dicts a real one would
    # produce, purely to test the timestamp-attribution algorithm itself
    # (multiple turns, cross-turn attribution) that one real, single-turn
    # session can't exercise either way.
    t0 = Turn("first", "", "2026-07-01T22:00:00Z", 0)
    t1 = Turn("second", "", "2026-07-01T22:10:00Z", 1)
    perms = [
        {
            "tool": "a",
            "decision": "approved",
            "feedback": "",
            "timestamp": "2026-07-01T22:05:00Z",
        },
        {
            "tool": "b",
            "decision": "denied",
            "feedback": "no",
            "timestamp": "2026-07-01T22:15:00Z",
        },
    ]
    attribute_permissions([t0, t1], perms)
    assert [p["tool"] for p in t0.permission_decisions] == ["a"]
    assert [p["tool"] for p in t1.permission_decisions] == ["b"]


def test_copilot_skills_from_events_are_attributed():
    lines = [
        json.dumps(
            {
                "type": "skill.invoked",
                "timestamp": "2026-07-01T22:05:00Z",
                "data": {
                    "name": "fixture-skill",
                    "path": "/repo/.agents/skills/fixture-skill/SKILL.md",
                },
            }
        )
    ]
    skills = copilot_log._skills_from_events(lines)
    assert skills == [
        {
            "name": "fixture-skill",
            "path": "/repo/.agents/skills/fixture-skill/SKILL.md",
            "timestamp": "2026-07-01T22:05:00Z",
        }
    ]
    turn = Turn("use the skill", "", "2026-07-01T22:00:00Z", 0)
    copilot_log.attribute_skills([turn], skills)
    assert turn.skills_used == ["fixture-skill"]


def test_copilot_permissions_from_events_join_decisions_and_pending():
    lines = [
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:03:50Z",
                "data": {
                    "requestId": "r1",
                    "permissionRequest": {
                        "kind": "shell",
                        "fullCommandText": "pytest",
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "permission.completed",
                "timestamp": "2026-07-01T22:04:04Z",
                "data": {"requestId": "r1", "result": {"kind": "approved"}},
            }
        ),
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:05:00Z",
                "data": {
                    "requestId": "r2",
                    "permissionRequest": {
                        "kind": "write",
                        "intention": "edit crud.py",
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "permission.completed",
                "timestamp": "2026-07-01T22:05:30Z",
                "data": {
                    "requestId": "r2",
                    "result": {
                        "kind": "denied-interactively-by-user",
                        "feedback": "not like that",
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:06:00Z",
                "data": {
                    "requestId": "r3",
                    "permissionRequest": {
                        "kind": "shell",
                        "fullCommandText": "rm x",
                    },
                },
            }
        ),
    ]
    perms = copilot_log._permissions_from_events(lines)
    assert [(p["tool"], p["decision"], p["feedback"]) for p in perms] == [
        ("pytest", "approved", ""),
        ("edit crud.py", "denied-interactively-by-user", "not like that"),
        ("rm x", "pending", ""),
    ]


def test_get_state_skills_real_fixture(monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    skills = copilot_log.get_state_skills(COPILOT_FIXTURE_SKILL_SESSION_ID)
    assert skills == [
        {
            "name": "fixture-skill",
            "path": "/redacted/skill-project/.github/skills/fixture-skill/SKILL.md",
            "timestamp": "2026-09-29T18:28:31.428Z",
        }
    ]


def test_get_state_permissions_real_fixture(monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    perms = copilot_log.get_state_permissions(COPILOT_FIXTURE_SKILL_SESSION_ID)
    assert perms == [
        {
            "tool": "Read file: /redacted/fixture.png",
            "decision": "denied-no-approval-rule-and-could-not-request-from-user",
            "feedback": "",
            "timestamp": "2026-09-29T18:28:52.142Z",
        }
    ]


def test_render_session_real_fixture_attributes_skill_and_permission(monkeypatch):
    # Real Copilot events carry the *completion* timestamp of the turn they
    # belong to, but a skill/permission event fires mid-turn -- before that
    # completion timestamp is written. attribute_skills/attribute_permissions
    # fold each event under the latest turn whose timestamp is <= the event's,
    # so both land one turn earlier than where a human would place them: the
    # skill invoked during turn 2 ("/fixture-skill") is attributed to turn 1,
    # and the permission decision made while handling turn 3 (the image
    # request) is attributed to turn 2. Real, verified behavior -- not a bug
    # this test is asserting around.
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        session = get_session_by_id(conn, COPILOT_FIXTURE_SKILL_SESSION_ID)
        markdown = render_session(conn, session)
    finally:
        conn.close()
    turn_1, turn_2, turn_3, _ = markdown.split("### Turn ")[1:5]
    assert "_Skill used:_ **fixture-skill**" in turn_1
    assert "_Skill used:_" not in turn_2
    assert "_Permission denied:_ **Read file: /redacted/fixture.png**" in turn_2
    assert "_Permission denied:_" not in turn_1 and "_Permission denied:_" not in turn_3


def test_get_session_files_and_files_changed_rendering_real_fixture(monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        files = copilot_log.get_session_files(conn, COPILOT_FIXTURE_SKILL_SESSION_ID)
        assert [f["file_path"] for f in files] == [
            "/redacted/skill-project/calculator.py",
            "/redacted/skill-project/test_calculator.py",
            "/redacted/copilot-home/session-state/"
            "7fc0ab52-6795-49e8-8ab9-5048733e64bc/plan.md",
        ]

        session = get_session_by_id(conn, COPILOT_FIXTURE_SKILL_SESSION_ID)
        markdown = render_session(conn, session)
    finally:
        conn.close()
    assert "## Files changed during the session" in markdown
    assert "/redacted/skill-project/calculator.py" in markdown
    # session_files' turn_index (1) lands the file-edit bullet under turn 2
    # ("/fixture-skill") in the detailed section, even though the edit was
    # actually restoring calculator.py during a later, unrecorded turn --
    # Copilot's "turns" table doesn't get a row for every user message (only
    # 4 rows exist here for 7 real prompts sent), so turn_index in
    # session_files can reference a turn the turns table never separately
    # recorded.
    turn_2 = markdown.split("\n## Turn ")[2]
    assert "apply_patch -> /redacted/skill-project/calculator.py" in turn_2


def test_get_session_refs_rendering_real_fixture(monkeypatch):
    # From a session where the agent actually ran `git commit`: real
    # confirmation that Copilot records the resulting hash in session_refs.
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        refs = copilot_log.get_session_refs(conn, COPILOT_FIXTURE_DENIAL_SESSION_ID)
        assert refs == [
            {
                "ref_type": "commit",
                "ref_value": "2c8f9bb",
                "turn_index": 5,
                "created_at": "2026-09-29T20:06:39.776Z",
            }
        ]

        session = get_session_by_id(conn, COPILOT_FIXTURE_DENIAL_SESSION_ID)
        markdown = render_session(conn, session)
    finally:
        conn.close()
    assert "## References" in markdown
    assert "- **commit**: `2c8f9bb` (turn 5)" in markdown


def test_denied_tool_calls_surface_the_real_denial_reason_real_fixture(monkeypatch):
    # A denied `tool.execution_complete` (a --deny-tool rule match) carries
    # no "result" field at all -- only "error" -- unlike a normal failure,
    # which does. _tools_from_events used to drop that error silently,
    # rendering just the bare word "error" with no explanation. Real
    # capture: one turn hit two different deny rules in a row (apply_patch
    # denied by `write`, then bash `rm` denied by `shell(rm)`), so this
    # checks both the raw parse and the rendered markdown.
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    tools = copilot_log.get_state_tools(COPILOT_FIXTURE_DENIAL_SESSION_ID)
    assert [(t["name"], t["success"], t["result"]) for t in tools] == [
        (
            "apply_patch",
            False,
            "Permission to run this tool was denied due to the following rules: `write`",
        ),
        (
            "bash",
            False,
            "Permission to run this tool was denied due to the following rules: `shell(rm)`",
        ),
    ]

    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        session = get_session_by_id(conn, COPILOT_FIXTURE_DENIAL_SESSION_ID)
        markdown = render_session(conn, session)
    finally:
        conn.close()
    assert (
        "apply_patch: error: Permission to run this tool was denied due to "
        "the following rules: `write`"
    ) in markdown
    assert (
        "bash: error: Permission to run this tool was denied due to the "
        "following rules: `shell(rm)`"
    ) in markdown


def test_real_attachment_without_bracket_token_is_attributed_to_its_turn(
    monkeypatch,
):
    # Real Copilot user.message events carry attachments alongside the exact
    # prompt text; they do not necessarily include the synthetic [image: name]
    # token used by older DB-only attachment records.
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    attachments = copilot_log.get_state_attachments(COPILOT_FIXTURE_SKILL_SESSION_ID)
    assert attachments == [
        {
            "display_name": "fixture.png",
            "path": "/redacted/fixture.png",
            "type": "image/png",
            "user_message": "Describe what is in the attached image, in one sentence.",
        }
    ]
    turns_raw = [
        {
            "turn_index": 2,
            "user_message": "Describe what is in the attached image, in one sentence.",
            "assistant_response": "x",
            "timestamp": "2026-09-29T18:28:54.115Z",
        }
    ]
    turns = copilot_log._build_turn_objects(turns_raw, [], attachments)
    assert turns[0].image_refs == [
        {
            "name": "fixture.png",
            "path": "/redacted/fixture.png",
            "type": "image/png",
        }
    ]
    # The checked-in fixture redacts the image file itself, so its reference is
    # retained as unavailable rather than silently disappearing.
    assert build_events(turns)[0]["images"] == [
        {"unavailable": True, "ref": "fixture.png"}
    ]


def test_build_events_inlines_resolved_image_and_keeps_unavailable_reference(
    tmp_path,
):
    image_path = tmp_path / "attached.png"
    image_path.write_bytes(base64.b64decode(_PNG_B64))
    turn = Turn("look at these", "", "2026-07-01T22:00:00Z", 0)
    turn.image_refs = [
        {"name": "attached.png", "path": str(image_path), "type": "image/png"},
        {"name": "missing.png"},
    ]

    user = build_events([turn])[0]
    assert base64.b64decode(user["images"][0]["data"]) == base64.b64decode(_PNG_B64)
    assert user["images"][0]["media_type"] == "image/png"
    assert user["images"][1] == {"unavailable": True, "ref": "missing.png"}


# checkpoints stayed empty across every real Copilot capture so far,
# including a --mode autopilot plan turn and several --continue resumes --
# `copilot <cmd> --help` has no checkpoint-related command either, so
# whatever triggers a numbered checkpoint looks to be an interactive-only
# feature not reachable from these headless -p runs. Hand-built schema is
# the only option until that changes.
# session_refs and session_files, by contrast, *are* real-fixture-backed
# now -- see test_get_session_refs_rendering_real_fixture and
# test_get_session_files_and_files_changed_rendering_real_fixture below.


def test_copilot_checkpoints_rendering():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    try:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                turn_index INTEGER NOT NULL,
                user_message TEXT,
                assistant_response TEXT,
                timestamp TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, turn_index)
            )
        """)
        cursor.execute("""
            CREATE TABLE session_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                file_path TEXT NOT NULL,
                tool_name TEXT,
                turn_index INTEGER,
                first_seen_at TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, file_path)
            )
        """)
        cursor.execute("""
            CREATE TABLE session_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                ref_type TEXT NOT NULL,
                ref_value TEXT NOT NULL,
                turn_index INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("""
            CREATE TABLE checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                checkpoint_number INTEGER NOT NULL,
                title TEXT,
                overview TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, checkpoint_number)
            )
        """)

        cursor.execute(
            "INSERT INTO sessions (id, cwd, repository, branch, summary, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "test-session-1",
                str(Path.cwd()),
                "test/repo",
                "main",
                "Test session",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:30:00Z",
            ),
        )
        cursor.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "test-session-1",
                0,
                "Hello, can you help?",
                "Of course! What do you need?",
                "2026-01-01T12:00:05Z",
            ),
        )
        cursor.execute(
            "INSERT INTO checkpoints "
            "(session_id, checkpoint_number, title, overview, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "test-session-1",
                1,
                "Add a test file",
                "Created a test file and confirmed it passes.",
                "2026-01-01T12:15:00Z",
            ),
        )
        conn.commit()
        conn.close()

        conn = get_db_connection(test_db)
        session = get_session_by_id(conn, "test-session-1")
        markdown = render_session(conn, session)
        conn.close()

        assert "## Checkpoints (1)" in markdown
        assert "### Checkpoint 1: Add a test file" in markdown
        assert "Created a test file and confirmed it passes." in markdown
    finally:
        test_db.unlink()


def test_copilot_prefix_matching():
    # Real fixture's session id is b10f9a1b-1803-4178-8fcc-8b2a15134624.
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        session = get_session_by_id(conn, COPILOT_FIXTURE_SESSION_ID)
        assert session is not None

        prefix_session = get_session_by_id(conn, COPILOT_FIXTURE_SESSION_ID[:8])
        assert prefix_session is not None
        assert prefix_session["id"] == COPILOT_FIXTURE_SESSION_ID
    finally:
        conn.close()


# Copilot's bracket-token image path -- [image: name] in the stored
# user_message text, joined against an attachments record by display
# name -- isn't exercised by the checked-in real fixture (see
# test_real_attachment_without_bracket_token_is_not_resolved_as_image
# above: real captures don't emit that bracket token). Covered here with
# hand-built schemas instead.


def test_copilot_image_reference_without_attachment_is_flagged():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    try:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                turn_index INTEGER NOT NULL,
                user_message TEXT,
                assistant_response TEXT,
                timestamp TEXT
            )
        """)
        cursor.execute(
            "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
        )
        cursor.execute(
            "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
        )
        cursor.execute(
            "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
        )

        cursor.execute(
            "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "img-session",
                str(Path.cwd()),
                "test/repo",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:00:00Z",
            ),
        )
        cursor.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                "img-session",
                0,
                "[image: copilot-image-test.png] please summarize",
                "Done",
                "2026-01-01T12:00:05Z",
            ),
        )
        conn.commit()
        conn.close()

        conn = get_db_connection(test_db)
        session = get_session_by_id(conn, "img-session")
        assert session is not None
        markdown = render_session(conn, session)
        assert "source file unavailable; description pending" in markdown
        conn.close()
    finally:
        test_db.unlink()


def test_copilot_image_attachment_is_dumped_to_marker_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "copilot-image-test.png"
        img_path.write_bytes(base64.b64decode(_PNG_B64))

        try:
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT,
                    repository TEXT,
                    branch TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_index INTEGER NOT NULL,
                    user_message TEXT,
                    assistant_response TEXT,
                    timestamp TEXT
                )
            """)
            cursor.execute(
                "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE attachments (session_id TEXT, display_name TEXT, path TEXT, type TEXT)"
            )

            cursor.execute(
                "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-2",
                    str(Path.cwd()),
                    "test/repo",
                    "2026-01-01T12:00:00Z",
                    "2026-01-01T12:00:00Z",
                ),
            )
            cursor.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-2",
                    0,
                    "[image: copilot-image-test.png] please summarize",
                    "Done",
                    "2026-01-01T12:00:05Z",
                ),
            )
            cursor.execute(
                "INSERT INTO attachments (session_id, display_name, path, type) VALUES (?, ?, ?, ?)",
                ("img-session-2", "copilot-image-test.png", str(img_path), "image/png"),
            )
            conn.commit()
            conn.close()

            conn = get_db_connection(test_db)
            session = get_session_by_id(conn, "img-session-2")
            assert session is not None
            markdown = render_session(conn, session)
            conn.close()

            marker = re.search(
                r"\[Image dumped to `([^`]+)` — description pending\]", markdown
            )
            assert marker is not None
            dumped = Path(marker.group(1))
            assert dumped.exists()
            dumped.unlink()
        finally:
            test_db.unlink()


def test_copilot_image_attachment_from_state_events_is_dumped_to_marker_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "copilot-image-test.png"
        img_path.write_bytes(base64.b64decode(_PNG_B64))

        state_root = Path(tmpdir) / "state-root"
        session_dir = state_root / "img-session-3"
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "data": {
                        "content": "[image: copilot-image-test.png] please summarize",
                        "attachments": [
                            {
                                "type": "file",
                                "path": str(img_path),
                                "displayName": "copilot-image-test.png",
                                "mimeType": "image/png",
                            }
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_root = copilot_log.COPILOT_STATE_ROOT
        copilot_log.COPILOT_STATE_ROOT = state_root
        try:
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT,
                    repository TEXT,
                    branch TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_index INTEGER NOT NULL,
                    user_message TEXT,
                    assistant_response TEXT,
                    timestamp TEXT
                )
            """)
            cursor.execute(
                "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
            )

            cursor.execute(
                "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-3",
                    str(Path.cwd()),
                    "test/repo",
                    "2026-01-01T12:00:00Z",
                    "2026-01-01T12:00:00Z",
                ),
            )
            cursor.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-3",
                    0,
                    "[image: copilot-image-test.png] please summarize",
                    "Done",
                    "2026-01-01T12:00:05Z",
                ),
            )
            conn.commit()
            conn.close()

            conn = get_db_connection(test_db)
            session = get_session_by_id(conn, "img-session-3")
            assert session is not None
            markdown = render_session(conn, session)
            conn.close()

            marker = re.search(
                r"\[Image dumped to `([^`]+)` — description pending\]", markdown
            )
            assert marker is not None
            dumped = Path(marker.group(1))
            assert dumped.exists()
            dumped.unlink()
        finally:
            copilot_log.COPILOT_STATE_ROOT = original_root
            test_db.unlink()


def test_build_events_recovers_tool_name_and_target():
    # The real fixture's turn has structured tool_calls (from the events.jsonl
    # sidecar), which build_events always prefers -- so it never exercises
    # this older tool_bullets-only fallback path. No image involved; a plain
    # turn with just a tool bullet is enough.
    turn = Turn("edit the model", "Done.", "2026-08-01T10:00:00Z", 0)
    turn.add_tool("str_replace_editor", "backend/app/models.py")
    events = build_events([turn])
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "str_replace_editor"
    assert call["input"] == {"target": "backend/app/models.py"}


def test_build_events_emits_an_assistant_event_for_a_reply():
    turn = Turn("look at this", "Sure, fixing.", "2026-08-01T10:00:00Z", 0)
    events = build_events([turn])
    assistant = [e for e in events if e["role"] == "assistant"]
    assert assistant and assistant[0]["text"] == "Sure, fixing."


def test_copilot_tools_from_events_join_start_and_complete():
    lines = [
        json.dumps(
            {
                "type": "tool.execution_start",
                "timestamp": "2026-07-01T22:00:01Z",
                "data": {
                    "toolCallId": "call_1",
                    "toolName": "grep",
                    "arguments": {"pattern": "audit"},
                },
            }
        ),
        json.dumps(
            {
                "type": "tool.execution_complete",
                "timestamp": "2026-07-01T22:00:02Z",
                "data": {
                    "toolCallId": "call_1",
                    "success": True,
                    "result": {"content": "match a\nmatch b", "detailedContent": "x"},
                },
            }
        ),
    ]
    tools = copilot_log._tools_from_events(lines)
    assert len(tools) == 1
    assert tools[0]["name"] == "grep"
    assert tools[0]["arguments"] == {"pattern": "audit"}
    assert tools[0]["success"] is True
    # detailedContent is preferred as the fuller result text.
    assert tools[0]["result"] == "x"


def test_copilot_attribute_tools_populates_bullet_note_and_envelope():
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
    assert turn.tool_bullets == ["- grep -> audit"]
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
    assert t0.tool_bullets == []
    assert t1.tool_bullets == ["- view -> a.py"]


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
    assert [e["role"] for e in events] == ["user", "assistant", "assistant", "assistant"]
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

    def fake_extract_all(conn, output, strict, raw, tool_result_max_bytes):
        called["output"] = output
        called["strict"] = strict
        return 0

    monkeypatch.setattr(copilot_log, "_extract_all", fake_extract_all)
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    rc = copilot_log.main(["--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert called == {"output": None, "strict": False}


# --------------------------------------------------------------------------- #
# Fixture-driven: real (trimmed, redacted) session-store.db + events.jsonl
# sidecar, real cwd-overlap --strict matching (no monkeypatch), and main()
# end-to-end.
# --------------------------------------------------------------------------- #


def test_matching_sessions_real_fixture_cwd_overlap_and_strict():
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        exact = matching_sessions(conn, Path("/redacted/project"), strict=True)
        assert [s["id"] for s in exact] == [COPILOT_FIXTURE_SESSION_ID]

        # A descendant cwd overlaps the recorded one but isn't an exact match.
        nested = Path("/redacted/project/sub")
        assert [s["id"] for s in matching_sessions(conn, nested, strict=False)] == [
            COPILOT_FIXTURE_SESSION_ID
        ]
        assert matching_sessions(conn, nested, strict=True) == []
    finally:
        conn.close()


def test_newest_session_real_fixture():
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        session = newest_session(conn, Path("/redacted/project"))
        assert session is not None
        assert session["id"] == COPILOT_FIXTURE_SESSION_ID
    finally:
        conn.close()


def test_state_sidecar_real_fixture_contains_tool_call(monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    tools = copilot_log.get_state_tools(COPILOT_FIXTURE_SESSION_ID)
    assert tools == [
        {
            "call_id": "call_gIWXOf0L79g8MtDAXdmEOQRB",
            "name": "rename_session",
            "arguments": {"title": "Local setup"},
            "timestamp": "2026-09-16T18:35:49.000Z",
            "success": True,
            "result": 'Renamed session to "Local setup".',
        }
    ]


def test_render_session_includes_real_events_sidecar_tool_call(monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    conn = get_db_connection(COPILOT_FIXTURE_DB)
    try:
        session = get_session_by_id(conn, COPILOT_FIXTURE_SESSION_ID)
        assert session is not None
        markdown = render_session(conn, session)
    finally:
        conn.close()
    assert "Please run the setup in this repo" in markdown
    assert "rename_session" in markdown


def test_main_writes_raw_envelope_from_real_events_sidecar(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    out_path = tmp_path / "out.md"
    rc = main(
        [
            "--db",
            str(COPILOT_FIXTURE_DB),
            COPILOT_FIXTURE_SESSION_ID,
            "--output",
            str(out_path),
        ]
    )
    assert rc == 0
    envelopes = list(tmp_path.glob("copilot_session_log_raw_*.json"))
    assert len(envelopes) == 1
    envelope = json.loads(envelopes[0].read_text(encoding="utf-8"))
    assert envelope["harness"] == "copilot"
    assert envelope["session_id"] == "2026-09-16_b10f9a1b"
    call = next(
        call
        for event in envelope["events"]
        for call in event.get("tool_calls", [])
    )
    assert call["name"] == "rename_session"
    assert call["input"] == {"title": "Local setup"}
    assert call["result"] == 'Renamed session to "Local setup".'


def test_main_end_to_end_with_real_fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", COPILOT_FIXTURE_STATE_ROOT)
    out_path = tmp_path / "out.md"
    rc = main(
        [
            "--db",
            str(COPILOT_FIXTURE_DB),
            COPILOT_FIXTURE_SESSION_ID,
            "--output",
            str(out_path),
            "--no-raw",
        ]
    )
    assert rc == 0
    assert out_path.is_file()
    markdown = out_path.read_text(encoding="utf-8")
    assert "Please run the setup in this repo" in markdown


def test_main_reports_missing_db_and_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    rc = main(["--db", str(missing)])
    assert rc == 1
    assert "not found" in capsys.readouterr().err



@pytest.fixture
def copilot_event_lines():
    """Mixed valid/invalid event fixture for sidecar join behavior."""
    events = [
        {"type": "permission.requested", "data": {"requestId": "pending"}},
        {
            "type": "permission.requested",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {
                "requestId": "done",
                "permissionRequest": {"intention": "edit"},
            },
        },
        {
            "type": "permission.completed",
            "timestamp": "2026-01-01T00:00:01Z",
            "data": {"requestId": "done", "result": {"kind": "approved"}},
        },
        {
            "type": "permission.completed",
            "data": {"requestId": "orphan", "result": {"kind": "denied"}},
        },
        {
            "type": "skill.invoked",
            "timestamp": "2026-01-01T00:00:02Z",
            "data": {"name": "audit", "path": 4},
        },
        {
            "type": "tool.execution_start",
            "timestamp": "2026-01-01T00:00:03Z",
            "data": {"toolCallId": "x", "toolName": "grep"},
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "x",
                "success": False,
                "error": {"message": "blocked"},
            },
        },
        {
            "type": "tool.execution_start",
            "data": {"toolCallId": "pending", "toolName": "view"},
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "pending",
                "success": True,
                "result": {"content": "fallback content"},
            },
        },
    ]
    return ["not json", "[]", *[json.dumps(event) for event in events]]


def test_copilot_sidecar_parser_tolerates_incomplete_and_alternate_records(
    copilot_event_lines,
):
    permissions = copilot_log._permissions_from_events(copilot_event_lines)
    assert [p["decision"] for p in permissions] == ["denied", "pending", "approved"]
    assert copilot_log._skills_from_events(copilot_event_lines) == [
        {
            "name": "audit",
            "path": None,
            "timestamp": "2026-01-01T00:00:02Z",
        }
    ]
    tools = copilot_log._tools_from_events(copilot_event_lines)
    assert [tool["result"] for tool in tools] == ["blocked", "fallback content"]
    assert copilot_log._result_text({"content": "fallback"}) == "fallback"
    assert copilot_log._result_text([1, 2]) == "[1, 2]"
    assert copilot_log._result_text(object()) == ""
    assert copilot_log.skill_refs_from_call(
        "mcp.skill", {"skill": "audit", "path": "/skills/audit/SKILL.md"}
    ) == [{"name": "audit", "path": "/skills/audit/SKILL.md"}]
    assert copilot_log.skill_refs_from_call("view", "no skill here") == []
    assert copilot_log._attachment_lookup(
        [{"display_name": "missing", "path": " "}, {"path": "/tmp/a.png"}]
    ) == {"a.png": {"type": "", "path": "/tmp/a.png"}}


def test_copilot_image_payload_decoding_and_database_schema_fallbacks(tmp_path):
    data_url = copilot_log._image_bytes_and_ext(
        {"data_url": "data:image/svg+xml;base64,QQ=="}
    )
    assert data_url == (b"A", "svg")
    assert copilot_log._image_bytes_and_ext({"data_url": "not an image"}) is None
    assert copilot_log._image_bytes_and_ext({"data": "%%% ", "media_type": "image/png"}) == (
        b"",
        "png",
    )
    assert copilot_log._image_bytes_and_ext({"data": "QQ==", "media_type": "custom"}) == (
        b"A",
        "img",
    )
    assert copilot_log._image_bytes_and_ext({"path": str(tmp_path / "lost.png")}) is None
    path_without_ext = tmp_path / "image"
    path_without_ext.write_bytes(b"raw")
    assert copilot_log._image_bytes_and_ext(
        {"path": str(path_without_ext), "type": "image/webp"}
    ) == (b"raw", "webp")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('one', '/work', 'repo', 'main', '', '1', '1')")
    assert copilot_log.get_session_attachments(conn, "one") == []
    assert copilot_log.get_session_models(conn, "one") == []
    assert copilot_log.newest_session(conn, Path("/elsewhere"), strict=True) is None
    assert copilot_log.newest_session(conn, Path("/elsewhere"))["id"] == "one"
    assert not copilot_log._cwd_matches(None, Path("/work"))
    assert copilot_log._table_columns(conn, "missing") == set()
    conn.close()


def test_copilot_state_reader_wrappers_and_model_lookup(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    event_path = state_root / "session" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text(
        "\n".join(
            [
                "{broken",
                json.dumps({"type": "user.message", "data": {
                    "content": "Look at this",
                    "attachments": [
                        {"displayName": "pic.png", "path": "/tmp/pic.png", "mimeType": "image/png", "dataUrl": "data:image/png;base64,QQ=="},
                        "not a record",
                        {"displayName": 3, "path": 4, "mimeType": 5, "data": "QQ==", "mediaType": "image/png"},
                    ],
                }}),
                json.dumps({"type": "skill.invoked", "data": "bad"}),
                json.dumps({"type": "permission.completed", "data": "bad"}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", state_root)
    attachments = copilot_log.get_state_attachments("session")
    assert attachments[0]["user_message"] == "Look at this"
    assert attachments[0]["data_url"] == "data:image/png;base64,QQ=="
    assert attachments[1]["display_name"] == ""
    assert attachments[1]["data"] == "QQ=="
    assert copilot_log.get_state_attachments("missing") == []
    assert copilot_log.get_state_permissions("missing") == []
    assert copilot_log.get_state_skills("missing") == []
    assert copilot_log.get_state_tools("missing") == []

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events (session_id TEXT, usage_model TEXT, timestamp TEXT)")
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?)",
        [
            ("session", "model-b", "1"),
            ("session", "model-a", "2"),
            ("session", "model-b", "3"),
            ("session", "", "4"),
        ],
    )
    assert copilot_log.get_session_models(conn, "session") == ["model-b", "model-a"]
    conn.close()


def test_copilot_rendering_empty_and_optional_sections():
    empty = copilot_log.render(
        {"id": "empty", "repository": "org/repo", "branch": "main", "summary": "done"},
        [],
        [],
        [],
        [],
        ["model"],
    )
    assert "No conversational turns" in empty
    assert "Repository: `org/repo`." in empty
    assert "Model: model." in empty

    turn = copilot_log.Turn("", "answer", None, 0)
    turn.permission_decisions = [
        {"decision": "approved", "tool": "shell", "feedback": "ok"}
    ]
    turn.result_notes = [f"result {i}" for i in range(8)]
    rendered = copilot_log.render(
        {"id": "one", "repository": "", "branch": "", "summary": ""},
        [turn],
        [{"checkpoint_number": 1, "title": "Next", "overview": "", "created_at": None}],
        [{"file_path": "a.py"}, {"file_path": ""}],
        [{"ref_type": "issue", "ref_value": "42", "turn_index": None}],
    )
    assert "no user text captured" in rendered
    assert "_permission approved:_ shell" in rendered
    assert "(+2 more results)" in rendered
    assert "`42`" in rendered
    assert "### Checkpoint 1: Next" in rendered


def test_copilot_session_cli_selectors_stdout_and_errors(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/redacted/project")))
    assert copilot_log.main(
        [
            "--db",
            str(COPILOT_FIXTURE_DB),
            "b10f9a1b",
            "--output",
            "-",
            "--no-raw",
        ]
    ) == 0
    assert "GitHub Copilot CLI Session" in capsys.readouterr().out

    assert copilot_log.main(
        ["--db", str(COPILOT_FIXTURE_DB), "missing-session"]
    ) == 1
    assert "no session found" in capsys.readouterr().err
    monkeypatch.setattr(copilot_log, "newest_session", lambda *args, **kwargs: None)
    assert copilot_log.main(
        ["--db", str(COPILOT_FIXTURE_DB), "--strict", "--output", "-"]
    ) == 1
    assert "exact cwd match only" in capsys.readouterr().err


def test_copilot_all_session_export_handles_empty_and_colliding_identifiers(
    monkeypatch, tmp_path, capsys
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sessions = [
        {
            "id": "abcdefgh-one",
            "cwd": "/work",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        },
        {
            "id": "abcdefgh-copy",
            "cwd": "/work",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-02",
        },
    ]
    monkeypatch.setattr(copilot_log, "matching_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(copilot_log, "render_session", lambda *args, **kwargs: "log")
    out = tmp_path / "out"
    assert copilot_log._extract_all(conn, str(out), raw=False) == 0
    assert sorted(path.name for path in out.glob("copilot_session_log_*.md")) == [
        "copilot_session_log_2026-01-01_abcdefgh-2.md",
        "copilot_session_log_2026-01-01_abcdefgh.md",
    ]
    monkeypatch.setattr(copilot_log, "matching_sessions", lambda *args, **kwargs: [])
    assert copilot_log._extract_all(conn, str(out)) == 1
    assert "no Copilot sessions" in capsys.readouterr().err
    conn.close()


def test_copilot_single_session_cli_reports_render_and_write_errors(monkeypatch, tmp_path, capsys):
    from test_extract_copilot_session_log import COPILOT_FIXTURE_DB

    output_dir = tmp_path / "directory"
    output_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/redacted/project")))
    monkeypatch.setattr(copilot_log, "render_session", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad session")))
    assert copilot_log.main(["--db", str(COPILOT_FIXTURE_DB), "b10f9a1b", "--output", "-", "--no-raw"]) == 1
    assert "bad session" in capsys.readouterr().err

    monkeypatch.undo()
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/redacted/project")))
    assert copilot_log.main(["--db", str(COPILOT_FIXTURE_DB), "b10f9a1b", "--output", str(output_dir), "--no-raw"]) == 1
    assert "could not write" in capsys.readouterr().err



if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        if inspect.signature(fn).parameters:
            continue  # needs pytest fixtures (monkeypatch/tmp_path/...); run via pytest
        fn()
        print(f"ok  {name}")
    print("all passed")
