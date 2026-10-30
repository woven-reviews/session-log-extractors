"""Contract tests for complete interpreted sessions and lossy JSON export."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.claude_sources import read_source
from session_log_extractors.session_log_envelope import (
    build_envelope,
    export_session,
    image_from_bytes,
    unavailable_image,
)
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    Event,
    Image,
    Session,
    ToolCall,
    UserMessage,
)

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
TRANSCRIPT = ROOT / "native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl"


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


FIXTURES = Path(__file__).parent / "fixtures"


def _native_records(relative_path):
    return [
        json.loads(line) for line in (FIXTURES / relative_path).read_text().splitlines()
    ]


@pytest.mark.parametrize(
    "source", ["candidate", "harness", "ide", "other_agent", "unknown"]
)
def test_authorship_is_independent_of_role_and_preserves_unicode(source):
    # Deliberately synthetic: exercise code points, including a combining mark,
    # rather than claiming these source classifications have native evidence.
    text = "é🙂e\u0301\ncontext"
    span = AuthorshipRange(
        start=0,
        end=4,
        source=source,
        markers=[
            AuthorshipMarker(field="native.source", value={"kind": [source, None]})
        ],
        ts="recorded-span-time",
    )
    event = UserMessage(
        i=0, role="user", text=text, ts="message-time", authorship=[span]
    )
    decoded = json.loads(json.dumps(event))
    assert decoded["text"][span["start"] : span["end"]] == "é🙂e\u0301"
    assert decoded["authorship"] == [span]
    assert decoded["text"] == text
    assert decoded["ts"] == "message-time"
    assert span["ts"] == "recorded-span-time"
    assert len(text.encode()) != len(text)


def test_authorship_examples_define_coverage_without_role_inference():
    documentation = (FIXTURES / "README.md").read_text()
    examples = json.loads(documentation.split("```json\n", 1)[1].split("```", 1)[0])
    candidate, mixed, injected, unknown, missing, partial = examples
    assert candidate["text"] == "Hi 🙂"
    assert candidate["authorship"] == [{"start": 0, "end": 4, "source": "candidate"}]
    assert [mixed["text"][r["start"] : r["end"]] for r in mixed["authorship"]] == [
        "Hi 🙂",
        "<notice>context</notice>",
    ]
    assert [r["source"] for r in mixed["authorship"]] == ["candidate", "harness"]
    assert injected["authorship"][0]["end"] == len(injected["text"])
    assert injected["authorship"][0]["source"] == "ide"
    assert unknown["authorship"][0]["source"] == "unknown"
    assert missing["role"] == "user" and "authorship" not in missing
    assert partial["text"][partial["authorship"][0]["end"] :] == " unknown"
    for event in examples:
        previous_end = 0
        for span in event.get("authorship", []):
            assert previous_end <= span["start"] < span["end"] <= len(event["text"])
            previous_end = span["end"]
        assert json.loads(json.dumps(event)) == event


def test_claude_native_injected_message_fits_authorship_contract():
    records = read_source(TRANSCRIPT).entries
    native = next(r for r in records if r.get("isMeta") and r.get("sourceToolUseID"))
    text = native["message"]["content"][0]["text"]
    span = AuthorshipRange(
        start=0,
        end=len(text),
        source="harness",
        ts=native["timestamp"],
        markers=[AuthorshipMarker(field="isMeta", value=native["isMeta"])],
    )
    event = UserMessage(
        i=0,
        role="user",
        id=native["uuid"],
        ts=native["timestamp"],
        text=text,
        authorship=[span],
    )
    assert json.loads(json.dumps(event))["text"] == text
    assert span["markers"] == [{"field": "isMeta", "value": True}]
    assert text.startswith("Base directory for this skill:")
    assert event["id"] == native["uuid"]


def test_codex_native_prompt_can_remain_unknown_without_evidence():
    records = _native_records("codex/slash.jsonl")
    native = next(
        r
        for r in records
        if r["type"] == "response_item" and r["payload"].get("role") == "user"
    )
    text = native["payload"]["content"][0]["text"]
    event = UserMessage(i=0, role="user", text=text, ts=native["timestamp"])
    assert json.loads(json.dumps(event))["text"] == "/review fixture"
    assert "authorship" not in event
    assert event["ts"] == native["timestamp"]


def test_copilot_native_transformed_prompt_fits_mixed_ranges():
    records = _native_records("copilot/baseline/events.jsonl")
    native = next(r for r in records if r["type"] == "user.message")
    data = native["data"]
    text = data["transformedContent"]
    boundary = len(text) - len(data["content"])
    # The suffix equality is evidence from this capture, not a classifier.
    assert text[boundary:] == data["content"]
    event = UserMessage(
        i=0,
        role="user",
        id=native["id"],
        message_id=data["messageId"],
        ts=native["timestamp"],
        text=text,
        authorship=[
            AuthorshipRange(
                start=0,
                end=boundary,
                source="harness",
                markers=[AuthorshipMarker(field="type", value=native["type"])],
            ),
            AuthorshipRange(start=boundary, end=len(text), source="candidate"),
        ],
    )
    decoded = json.loads(json.dumps(event))
    assert decoded["text"][:boundary].startswith("<current_datetime>")
    assert decoded["text"][boundary:] == data["content"]
    assert decoded["authorship"] == event["authorship"]
    assert decoded["message_id"] == data["messageId"]


def test_authorship_retains_message_structure_without_changing_export():
    events = [
        Event(
            i=0,
            role="user",
            text=None,
            images=[Image(unavailable=True, ref="image.png")],
            authorship=[],
        ),
        Event(
            i=1,
            role="assistant",
            text="reply",
            tool_calls=[ToolCall(name="Read", id="call-1")],
            authorship=[AuthorshipRange(start=0, end=5, source="unknown", ts=None)],
        ),
        Event(i=2, role="user", text="", authorship=[]),
    ]
    session = Session(harness="test", session_id="s", events=events)
    original = deepcopy(session)
    decoded = json.loads(json.dumps(session))
    assert decoded == session
    assert decoded["events"][0]["images"] == events[0]["images"]
    assert decoded["events"][1]["tool_calls"][0]["id"] == "call-1"
    assert decoded["events"][1]["authorship"][0]["ts"] is None
    exported = export_session(session)
    assert all("authorship" not in event for event in exported["events"])
    assert [event["i"] for event in exported["events"]] == [0, 1, 2]
    assert session == original
