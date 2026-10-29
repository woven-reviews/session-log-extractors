"""Contract tests for complete interpreted sessions and lossy JSON export."""

import json
from copy import deepcopy

import pytest

from session_log_extractors.session_log_envelope import (
    build_envelope,
    export_session,
    image_from_bytes,
    unavailable_image,
)
from session_log_extractors.session_model import Event, Image, Session, ToolCall


@pytest.mark.parametrize("harness", ["claude", "codex", "copilot"])
def test_shared_session_preserves_complete_content(harness):
    images = [
        Image(**image_from_bytes(b"image", "image/png")),
        Image(**unavailable_image("missing.png")),
    ]
    call = ToolCall(
        name="Read",
        input={"paths": ["a", "b"], "options": {"limit": None}},
        result={"content": [None, "", {"text": "original"}]},
        is_error=True,
    )
    call["error"] = {"code": 42, "details": [None, {}]}
    events = [
        Event(i=0, ts=None, role="user", text="**hello**", images=images),
        Event(i=1, ts="later", role="assistant", text="", tool_calls=[call]),
    ]
    session = Session(
        harness=harness,
        session_id="s1",
        cwd="/work",
        started_at="start",
        models=["model-a", "model-b", "model-a"],
        events=events,
    )
    expected = {
        "schema": "raw-session-log/1",
        **session,
        "truncation": {"tool_result_max_bytes": 4096},
    }
    assert build_envelope(session) == expected
    assert json.loads(json.dumps(export_session(session))) == expected


@pytest.mark.parametrize("limit", [4096, 0, 4, -1])
def test_export_caps_only_the_copy_and_retains_original_size(limit):
    result = "€" * 3000
    session = Session(
        harness="test",
        session_id="full",
        events=[
            Event(
                i=0,
                ts=None,
                role="assistant",
                text="",
                tool_calls=[
                    ToolCall(
                        name="Read",
                        input={"full": result},
                        result=result,
                        is_error=False,
                    )
                ],
            )
        ],
    )
    original = deepcopy(session)
    exported = export_session(session, limit)
    call = exported["events"][0]["tool_calls"][0]
    assert call["result_bytes"] == 9000
    assert call["result_truncated"] is (limit >= 0)
    expected = result if limit < 0 else result.encode()[:limit].decode(errors="ignore")
    assert call["result"] == expected
    assert call["input"] == {"full": result}
    assert call["is_error"] is False
    assert exported["truncation"] == {"tool_result_max_bytes": limit}
    assert session == original
    assert export_session(session, -1)["events"][0]["tool_calls"][0]["result"] == result
    call["input"]["full"] = "changed"
    assert session == original


def test_export_default_and_missing_null_empty_distinctions():
    calls = [
        ToolCall(name="missing"),
        ToolCall(name="null", input=None, result=None),
        ToolCall(name="empty", input={}, result=""),
        ToolCall(name="long", result="x" * 5000),
    ]
    events = [
        Event(i=0, role="user"),
        Event(i=1, role="user", ts=None, text=None, images=None),
        Event(i=2, role="user", ts="", text="", images=[]),
        Event(i=3, role="assistant", tool_calls=[]),
        Event(i=4, role="assistant", tool_calls=calls),
    ]
    exported = export_session(Session(harness="test", session_id="s", events=events))
    assert exported["cwd"] is None
    assert exported["started_at"] is None
    assert exported["models"] == []
    assert exported["events"][:4] == events[:4]
    actual = exported["events"][4]["tool_calls"]
    assert actual[0] == {"name": "missing"}
    assert actual[1] == {
        "name": "null",
        "input": None,
        "result": None,
        "result_bytes": 0,
        "result_truncated": False,
    }
    assert actual[2] == {
        "name": "empty",
        "input": {},
        "result": "",
        "result_bytes": 0,
        "result_truncated": False,
    }
    assert actual[3]["result"] == "x" * 4096
    assert actual[3]["result_bytes"] == 5000
    assert actual[3]["result_truncated"] is True


def test_export_preserves_explicit_null_and_empty_metadata():
    session = Session(
        harness="test", session_id="", cwd="", started_at="", models=None, events=[]
    )
    exported = export_session(session)
    assert exported["cwd"] == ""
    assert exported["started_at"] == ""
    assert exported["models"] is None
    assert exported["events"] == []


@pytest.mark.parametrize("limit", [2, -1])
def test_export_preserves_recorded_result_provenance(limit):
    call = ToolCall(
        name="Read", result="retained", result_bytes=9000, result_truncated=True
    )
    session = Session(
        harness="test",
        session_id="s",
        events=[
            Event(i=0, role="assistant", tool_calls=[call]),
            Event(i=1, role="assistant", tool_calls=None),
        ],
    )
    exported = export_session(session, limit)
    actual = exported["events"][0]["tool_calls"][0]
    assert actual["result_bytes"] == 9000
    assert actual["result_truncated"] is True
    assert actual["result"] == ("re" if limit == 2 else "retained")
    assert exported["events"][1]["tool_calls"] is None
    assert call["result"] == "retained"


def test_export_reads_json_fields_without_mutating_session_semantics():
    session = Session(
        harness="claude",
        session_id="s",
        events=[],
        skills_used=["fixture-skill"],
        subagent_count=1,
        unmatched_tool_results=[],
        files_changed=["/workspace/app.py"],
        permission_decisions=[
            {
                "tool": "read app.py",
                "request_id": "request-1",
                "status": "completed",
                "native_decision": "source-specific-label",
                "feedback": "",
                "ts": "recorded time",
            }
        ],
        file_activity=[
            {
                "file_path": "/workspace/app.py",
                "tool_name": "Read",
                "provenance": {
                    "record_type": "claude.tool_use",
                    "turn_index": None,
                    "first_seen_at": None,
                },
            }
        ],
    )
    original = deepcopy(session)
    assert export_session(session) == {
        "schema": "raw-session-log/1",
        "harness": "claude",
        "session_id": "s",
        "cwd": None,
        "started_at": None,
        "models": [],
        "events": [],
        "truncation": {"tool_result_max_bytes": 4096},
    }
    assert session == original


def test_export_preserves_payloads_but_leaves_model_semantics_unused():
    call = ToolCall(
        name="Read",
        id="call-1",
        input={"file_path": "/app.py", "extension": {"unknown": [1, None]}},
        result="full",
        result_record={
            "tool_call_id": "call-1",
            "ts": "later",
            "content": "full",
            "is_error": False,
        },
        permission_denial={"tool": "Read", "message": "no", "by": "user"},
    )
    event = Event(
        i=0,
        role="assistant",
        id="native-message",
        parent_id="parent",
        tool_calls=[call],
    )
    session = Session(harness="claude", session_id="s", events=[event])
    original = deepcopy(session)
    exported = export_session(session)
    assert exported["events"] == [
        {
            "i": 0,
            "role": "assistant",
            "tool_calls": [
                {
                    "name": "Read",
                    "input": call["input"],
                    "result": "full",
                    "result_bytes": 4,
                    "result_truncated": False,
                }
            ],
        }
    ]
    assert session == original
