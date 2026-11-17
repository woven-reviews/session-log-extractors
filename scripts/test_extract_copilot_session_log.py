"""Tests for extract_copilot_session_log.

Coverage: pytest --cov=scripts --cov-report=term-missing \
    scripts/test_extract_copilot_session_log.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from copilot_fixture import (
    ASSETS,
    DATABASE,
    ROOT,
    SESSION_ID,
    native_prose_source,
    native_records,
    native_source,
    native_tool_source,
    recorded_calls,
    single_image_source,
    write_events_log,
)

from session_log_extractors import copilot_protocol
from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot_log
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


def _hand_lines(name: str) -> list[str]:
    return (FIXTURES / "boundary" / name).read_text(encoding="utf-8").splitlines()


@pytest.fixture(autouse=True)
def _isolate_copilot_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "COPILOT_STATE_ROOT", tmp_path / "session-state")


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
    actual_output_identifier = copilot_log.output_identifier(
        sources.CopilotSession(Path("unused"), "abcdef1234")
    )
    assert actual_output_identifier == "abcdef12"


# -- session extraction integration ------------------------------------------


# -- _extract_all --------------------------------------------------------------


# -- main() ---------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", [["--raw"], ["--no-raw"], ["--raw-tool-result-bytes", "3"]]
)
def test_copilot_cli_rejects_removed_flags(flag):
    with pytest.raises(SystemExit, match="2"):
        copilot_log.build_arg_parser().parse_args(flag)


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


def test_native_messages_keep_order_identity_and_complete_results():
    source = native_tool_source()
    # Enlarge the captured result, retaining the native call/message links.
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
    expected_result = copilot_log._result_text(result)
    assert call["result"] == expected_result
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
    source = native_source()
    # Controlled native shell evidence replaces the retired DB display markup.
    source.state_records = [
        {
            "type": "tool.user_requested",
            "data": {"toolCallId": "shell", "arguments": {"command": "pytest -q"}},
        }
    ]
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


def _session(root, session_id, *, start="2026-01-01T00:00:00Z", cwd=None):
    """Change only lifecycle metadata in a complete native capture copy."""
    records = native_records()
    for record in records:
        if record["type"] == "session.start":
            record["data"]["sessionId"] = session_id
            record["data"]["startTime"] = start
            record["data"]["context"]["cwd"] = str(cwd or root)
    lines = []
    for record in records:
        lines.append(json.dumps(record))
    return write_events_log(root / ".copilot" / "session-state", session_id, lines)


def _cli(root, *args):
    script = Path(__file__).parent / "extract_copilot_session_log.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=root,
        env={**os.environ, "HOME": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("database", ["missing", "no-row", "corrupt"])
@pytest.mark.parametrize("prefix", [False, True])
def test_event_only_extraction_ignores_database(tmp_path, database, prefix):
    _session(tmp_path, SESSION_ID)
    path = tmp_path / ".copilot" / "session-store.db"
    if database == "no-row":
        shutil.copyfile(DATABASE, path)
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM sessions")
    elif database == "corrupt":
        path.write_text("unreadable as SQLite")
    selector = SESSION_ID[:8] if prefix else SESSION_ID

    result = _cli(tmp_path, selector, "--output", "-")

    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["session_id"] == SESSION_ID
    assert envelope["events"]
    assert envelope["cwd"] == str(tmp_path)
    assert envelope["started_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("selector", ["ab", "missing", "ABC", "a%", "a_"])
def test_selection_failure_never_overwrites(tmp_path, selector):
    _session(tmp_path, "abc")
    _session(tmp_path, "abc-later")
    output = tmp_path / "export.json"
    output.write_text("original")

    result = _cli(tmp_path, selector, "--output", str(output))

    assert result.returncode == 1
    assert result.stdout == ""
    assert selector in result.stderr
    assert output.read_text() == "original"


def test_exact_match_wins_over_prefix(tmp_path):
    _session(tmp_path, "abc-later")
    _session(tmp_path, "abc")
    result = _cli(tmp_path, "abc", "--output", "-")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["session_id"] == "abc"


@pytest.mark.parametrize("batch_args", [[], ["--all"]])
def test_batch_discovers_event_only_sessions(tmp_path, batch_args):
    _session(tmp_path, "abc")
    _session(tmp_path, "abc-later")
    result = _cli(tmp_path, *batch_args, "--output", str(tmp_path))
    assert result.returncode == 0, result.stderr
    identities = set()
    for path in tmp_path.glob("*.json"):
        identities.add(json.loads(path.read_text())["session_id"])
    assert identities == {"abc", "abc-later"}


def test_recent_activity_beats_creation_time(tmp_path):
    old = _session(tmp_path, "older", start="2025-01-01T00:00:00Z")
    _session(tmp_path, "newer", start="2026-01-01T00:00:00Z")
    database = tmp_path / ".copilot" / "session-store.db"
    shutil.copyfile(DATABASE, database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET id = 'newer'")
    # A resumed older session has the most recent recorded activity.
    with old.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "session.resume",
                    "timestamp": "2027-01-01T00:00:00Z",
                    "data": {"context": {"cwd": "/other"}},
                }
            )
            + "\n"
        )
    result = _cli(tmp_path, "--output", "-")
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["session_id"] == "older"
    assert envelope["started_at"] == "2025-01-01T00:00:00Z"
    assert envelope["cwd"] == str(tmp_path)


def test_unrelated_invalid_source_does_not_block_explicit_selection(tmp_path):
    _session(tmp_path, "valid")
    write_events_log(tmp_path / ".copilot" / "session-state", "broken", ["invalid"])
    result = _cli(tmp_path, "valid", "--output", "-")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["events"]


def _installed_session(tmp_path, session_id=SESSION_ID):
    """Install a complete capture whose recorded start cwd matches the test."""
    path = _session(tmp_path, session_id, cwd=Path.cwd())
    return path.parent.parent


@pytest.mark.parametrize("mode", ["stdout", "file", "default", "batch"])
def test_event_cli_preserves_full_identity_in_every_destination(tmp_path, capsys, mode):
    root = _installed_session(tmp_path)
    output = tmp_path / "exports"
    output.mkdir()
    args = ["--state-root", str(root)]
    if mode != "batch":
        args.append(SESSION_ID)
    if mode == "stdout":
        args.extend(["--output", "-"])
    elif mode == "file":
        args.extend(["--output", str(output / "custom.json")])

    status = copilot_log.main(args, project_root=output)

    captured = capsys.readouterr()
    assert status == 0, captured.err
    if mode == "stdout":
        envelope = json.loads(captured.out)
        assert list(output.iterdir()) == []
    else:
        assert captured.out == ""
        (path,) = output.iterdir()
        envelope = json.loads(path.read_text())
        expected_name = (
            "custom.json"
            if mode == "file"
            else "copilot_session_log_raw_2026-01-01_07640183.json"
        )
        assert path.name == expected_name
    assert envelope["session_id"] == SESSION_ID
    assert envelope["events"]


@pytest.mark.parametrize("explicit_root", [True, False])
def test_event_batch_preserves_collisions_and_default_output(
    tmp_path, monkeypatch, explicit_root
):
    monkeypatch.chdir(tmp_path)
    root = _installed_session(tmp_path, "abcdefgh-first")
    _installed_session(tmp_path, "abcdefgh-second")
    output = tmp_path / "exports" if explicit_root else tmp_path
    status = copilot_log.main(
        ["--all", "--state-root", str(root), "--output", "-"],
        project_root=output if explicit_root else None,
    )
    assert status == 0
    names = set()
    identities = set()
    for path in output.glob("*.json"):
        names.add(path.name)
        identities.add(json.loads(path.read_text())["session_id"])
    assert names == {
        "copilot_session_log_raw_2026-01-01_abcdefgh.json",
        "copilot_session_log_raw_2026-01-01_abcdefgh-2.json",
    }
    assert identities == {"abcdefgh-first", "abcdefgh-second"}


@pytest.mark.parametrize("destination", ["selected", "newest", "batch"])
def test_empty_discovery_fails_without_output(tmp_path, capsys, destination):
    output = tmp_path / "output"
    args = ["--state-root", str(tmp_path / "missing")]
    if destination != "batch":
        args.extend(["--output", "-"])
    if destination == "selected":
        args.append("missing")
    status = copilot_log.main(args, project_root=output)
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "error:" in captured.err
    assert not output.exists()


@pytest.mark.parametrize("batch", [False, True])
def test_strict_selection_has_no_global_fallback(tmp_path, monkeypatch, capsys, batch):
    root = _installed_session(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = ["--state-root", str(root), "--strict"]
    if not batch:
        args.extend(["--output", "-"])
    status = copilot_log.main(args, project_root=tmp_path / "exports")
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "no Copilot sessions" in captured.err


@pytest.mark.parametrize("problem", ["invalid", "unreadable", "directory"])
def test_invalid_selected_candidate_has_visible_diagnostic(
    tmp_path, monkeypatch, capsys, problem
):
    root = _installed_session(tmp_path)
    path = root / SESSION_ID / "events.jsonl"
    if problem == "invalid":
        path.write_text("invalid\n" * 8)
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    else:
        original = Path.open

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("access denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    status = copilot_log.main([SESSION_ID, "--state-root", str(root), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert str(path) in captured.err
    assert "warning:" in captured.err


@pytest.mark.parametrize("failure", ["source", "output"])
def test_event_batch_attempts_all_selected_sources(
    tmp_path, monkeypatch, capsys, failure
):
    root = _installed_session(tmp_path, "first")
    _installed_session(tmp_path, "broken")
    _installed_session(tmp_path, "last")
    output = tmp_path / "exports"
    output.mkdir()
    broken_output = output / "copilot_session_log_raw_2026-01-01_broken.json"
    if failure == "source":
        original = copilot_log.read_source

        def failed_read(path):
            if path.session.session_id == "broken":
                raise PermissionError("selected source denied")
            return original(path)

        monkeypatch.setattr(copilot_log, "read_source", failed_read)
    else:
        broken_output.mkdir()
    status = copilot_log.main(["--state-root", str(root), "--output", str(output)])
    captured = capsys.readouterr()
    assert status == 1
    assert "done: 2/3 session(s) written" in captured.err
    for identifier in ("first", "last"):
        path = output / f"copilot_session_log_raw_2026-01-01_{identifier}.json"
        assert json.loads(path.read_text())["events"]
    assert not broken_output.is_file()


def test_selected_source_error_remains_visible(tmp_path, monkeypatch, capsys):
    root = _installed_session(tmp_path)

    def denied(path):
        raise PermissionError("selected source denied")

    monkeypatch.setattr(copilot_log, "read_source", denied)
    status = copilot_log.main([SESSION_ID, "--state-root", str(root), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "selected source denied" in captured.err


def test_event_batch_output_directory_error(tmp_path, capsys):
    root = _installed_session(tmp_path)
    output = tmp_path / "file"
    output.touch()
    status = copilot_log.main(["--state-root", str(root), "--output", str(output)])
    captured = capsys.readouterr()
    assert status == 1
    assert "error:" in captured.err


def test_event_cli_stdout_error(tmp_path, monkeypatch, capsys):
    root = _installed_session(tmp_path)
    output = Mock()
    output.write.side_effect = BrokenPipeError("closed stdout")
    monkeypatch.setattr(copilot_log.sys, "stdout", output)
    status = copilot_log.main([SESSION_ID, "--state-root", str(root), "--output", "-"])
    captured = capsys.readouterr()
    assert status == 1
    assert "closed stdout" in captured.err


@pytest.mark.parametrize("cap", [0, 17, -1])
def test_event_cli_cap_and_size_warning(tmp_path, monkeypatch, capsys, cap):
    root = _installed_session(tmp_path)
    monkeypatch.setattr(
        "session_log_extractors.session_log_envelope.SIZE_WARN_BYTES", 1
    )
    status = copilot_log.main(
        [
            SESSION_ID,
            "--state-root",
            str(root),
            "--output",
            "-",
            "--tool-result-bytes",
            str(cap),
        ]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert "--tool-result-bytes" in captured.err
    envelope = json.loads(captured.out)
    assert envelope["truncation"]["tool_result_max_bytes"] == cap
    calls = []
    for message in envelope["events"]:
        calls.extend(message.get("tool_calls", []))
    assert calls
    truncated = []
    for call in calls:
        if isinstance(call["result"], str):
            if cap >= 0:
                assert len(call["result"].encode()) <= cap
            if call["result_truncated"]:
                truncated.append(call)
    assert bool(truncated) is (cap >= 0)


def test_event_cli_help_and_removed_database_flag(capsys):
    with pytest.raises(SystemExit, match="0"):
        copilot_log.main(["--help"])
    help_text = capsys.readouterr().out
    assert "events: []" in help_text
    assert "exit 1" in help_text
    assert "--state-root" in help_text
    with pytest.raises(SystemExit, match="2"):
        copilot_log.main(["--db", "missing.db"])


def test_response_only_results_retain_native_order_and_question_addresses():
    source = native_source()
    responses = []
    for record in source.state_records:
        if (
            record["type"] == "human_response.recorded"
            and record["data"]["response"]["responseKind"] == "ask_user"
        ):
            responses.append(record)
    assert responses, "Capture must contain structured question responses"
    # Break only one recorded tool relationship. Its response stays explicitly
    # unassociated while the other questions keep their message addresses.
    response = responses[0]
    response["data"]["toolCallId"] = "unmatched-controlled-question"
    session = copilot_log.interpret_source(source)
    result_messages = []
    for message in session["events"]:
        if message.get("id") == response["id"]:
            result_messages.append(message)
    assert len(result_messages) == 1
    assert json.loads(result_messages[0]["tool_calls"][0]["result"]) == response["data"]
    assert session["interaction_questions"]
    associated_responses = []
    for question in session["interaction_questions"]:
        activity = question["activity"]
        message = session["events"][activity["message_i"]]
        assert message["tool_calls"][activity["tool_call_index"]]["name"] == "ask_user"
    for answer in session["question_responses"]:
        if "activity" in answer:
            associated_responses.append(answer)
    assert associated_responses


@pytest.mark.parametrize("destination", ["stdout", "file", "batch"])
def test_lifecycle_only_session_exports_empty_conversation(
    tmp_path, capsys, destination
):
    root = _installed_session(tmp_path, "empty")
    path = root / "empty" / "events.jsonl"
    starts = []
    for record in native_records():
        if record["type"] == "session.start":
            record["data"]["sessionId"] = "empty"
            record["data"]["context"]["cwd"] = str(Path.cwd())
            starts.append(json.dumps(record))
    path.write_text("\n".join(starts) + "\n")
    args = ["--state-root", str(root)]
    output = tmp_path / "exports"
    if destination != "batch":
        args.append("empty")
        args.extend(["--output", "-" if destination == "stdout" else str(output)])
    status = copilot_log.main(args, project_root=output)
    captured = capsys.readouterr()
    assert status == 0, captured.err
    if destination == "stdout":
        envelope = json.loads(captured.out)
    elif destination == "file":
        envelope = json.loads(output.read_text())
    else:
        (written,) = output.iterdir()
        envelope = json.loads(written.read_text())
    assert envelope["session_id"] == "empty"
    assert envelope["events"] == []


@pytest.mark.parametrize("start_available", [True, False])
def test_cli_resume_cwd_only_fills_missing_start_cwd(tmp_path, start_available):
    path = _session(tmp_path, "resumed")
    lines = []
    # Use the captured resume, changing only its cwd and the availability of start cwd.
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record["type"] == "session.start" and not start_available:
            record["data"]["context"].pop("cwd")
        elif record["type"] == "session.resume":
            record["data"]["context"]["cwd"] = (
                str(tmp_path) if not start_available else "/elsewhere"
            )
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")
    result = _cli(tmp_path, "--strict", "--output", "-")
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["session_id"] == "resumed"
    assert envelope["cwd"] == str(tmp_path)
    assert envelope["started_at"] == "2026-01-01T00:00:00Z"


def test_discovery_reuses_snapshot_and_bounds_malformed_record_diagnostics(
    tmp_path, monkeypatch, capsys
):
    root = _installed_session(tmp_path)
    path = root / SESSION_ID / "events.jsonl"
    expected = copilot_log.build_raw_envelope(copilot_log.read_source(path))
    original_text = path.read_text()
    path.write_text("\n42\n" + original_text + "bad\n" * 6)
    original_open = Path.open
    reads = []

    def read(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read)

    status = copilot_log.main([SESSION_ID, "--state-root", str(root), "--output", "-"])

    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out) == expected
    assert reads == [path]
    warnings = []
    for line in captured.err.splitlines():
        if line.startswith("warning:"):
            warnings.append(line)
    assert len(warnings) == 6
    assert f"{path}: skipping non-object JSON on line 2" in warnings[0]
    assert "skipped 7 malformed/non-object records total" in warnings[-1]


def test_first_completed_turn_exports_while_session_is_running(tmp_path):
    path = _session(tmp_path, "first-turn")
    lines = []
    first_user = None
    completed_reply = None
    # Cut the committed capture immediately after its first completed response,
    # before the next prompt, resume, shutdown or database indexing is available.
    for line in path.read_text().splitlines():
        record = json.loads(line)
        lines.append(line)
        if record["type"] == "user.message" and first_user is None:
            first_user = record
        data = record.get("data", {})
        if (
            first_user
            and record["type"] == "assistant.message"
            and data.get("content")
            and not data.get("toolRequests")
        ):
            completed_reply = record
            break
    assert first_user is not None, "Capture must contain a human prompt"
    assert completed_reply is not None, "Capture must contain a completed reply"
    path.write_text("\n".join(lines) + "\n")

    result = _cli(tmp_path, "first-turn", "--output", "-")

    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    users = []
    for message in envelope["events"]:
        if message["role"] == "user":
            users.append(message)
    assert len(users) == 1
    assert users[0]["submitted"]["text"] == first_user["data"]["content"]
    assert envelope["events"][-1]["ts"] == completed_reply["timestamp"]
    assert envelope["events"][-1]["text"] == completed_reply["data"]["content"]
    assert not (tmp_path / ".copilot" / "session-store.db").exists()
