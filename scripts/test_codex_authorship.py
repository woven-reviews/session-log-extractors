"""Authorship contracts against the complete interactive Codex capture."""

import json
from copy import deepcopy

import codex_capture as capture
import pytest

from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors.codex_authorship import message_authorship
from session_log_extractors.session_log_envelope import export_session

META = "internal_chat_message_metadata_passthrough"


def _source():
    return codex.read_source(capture.TRANSCRIPT)


def _message(session, native):
    for message in session["events"]:
        if message.get("id") == native["payload"]["id"]:
            return message
    raise AssertionError("Native message was lost during interpretation")


def _sources(message):
    sources = []
    for span in message["authorship"]:
        sources.append(span["source"])
    return sources


def _native_kind(source, kind):
    for entry in capture.response_items(source.entries, "message"):
        metadata = entry["payload"].get(META, {})
        if metadata.get("content_item_kinds") == [kind]:
            return entry
    raise AssertionError(f"Capture lacks required native content kind: {kind}")


def test_native_candidate_and_injected_blocks_reach_production_json(capsys):
    source = _source()
    native = capture.human_message(source.entries)
    context = _native_kind(source, "environments.environment_context")
    session = codex.interpret_source(source)
    candidate = _message(session, native)
    injected = _message(session, context)
    assert candidate["text"] == native["payload"]["content"][0]["text"]
    assert candidate["turn_id"] == native["payload"][META]["turn_id"]
    assert _sources(candidate) == ["candidate"]
    assert injected["text"] == context["payload"]["content"][0]["text"]
    assert _sources(injected) == ["harness"]
    span = injected["authorship"][0]
    assert span["ts"] == context["timestamp"]
    assert span["markers"]
    original = deepcopy(session)
    result = codex.main(["--transcript", str(capture.TRANSCRIPT), "--output", "-"])
    assert result == 0
    cli_events = json.loads(capsys.readouterr().out)["events"]
    exported = export_session(session)
    assert exported["events"] == cli_events
    assert cli_events[candidate["i"]]["authorship"] == candidate["authorship"]
    assert cli_events[injected["i"]]["authorship"] == injected["authorship"]
    assert session == original


def test_mixed_native_blocks_keep_codepoint_offsets_and_recorded_markers():
    source = _source()
    native = capture.human_message(source.entries)
    context = _native_kind(source, "environments.environment_context")
    # Combine captured blocks, changing only candidate text for the Unicode edge.
    payload = native["payload"]
    payload["content"][0]["text"] = "Hi 🙂"
    payload["content"].append(deepcopy(context["payload"]["content"][0]))
    for key in ["content_item_kinds", "content_item_metadata"]:
        payload[META][key].append(deepcopy(context["payload"][META][key][0]))
    session = codex.interpret_source(source)
    message = _message(session, native)
    first, second = message["authorship"]
    assert (first["start"], first["end"], first["source"]) == (0, 4, "candidate")
    assert second["start"] == 5 and second["end"] == len(message["text"])
    assert second["source"] == "harness"
    assert message["text"][:4] == "Hi 🙂"
    assert message["text"][5:] == payload["content"][1]["text"]
    expected_kind = {
        "field": f"payload.{META}.content_item_kinds[1]",
        "value": "environments.environment_context",
    }
    expected_provenance = {
        "field": f"payload.{META}.content_item_metadata[1].provenance",
        "value": {"type": "harness", "hook": False},
    }
    assert expected_kind in second["markers"]
    assert expected_provenance in second["markers"]


@pytest.mark.parametrize(
    "change", ["missing", "unknown_kind", "unknown_source", "misaligned", "conflict"]
)
def test_unsupported_evidence_stays_unknown_and_preserved(change):
    source = _source()
    native = capture.human_message(source.entries)
    meta = native["payload"][META]
    if change == "missing":
        native["payload"].pop(META)
    elif change == "unknown_kind":
        meta["content_item_kinds"] = ["future.content"]
    elif change == "unknown_source":
        meta["content_item_metadata"][0]["provenance"] = {
            "type": "future",
            "detail": [None, 1],
        }
    elif change == "misaligned":
        meta["content_item_kinds"].append("environments.environment_context")
    else:
        meta["content_item_metadata"][0]["provenance"] = {
            "type": "harness",
            "hook": False,
        }
    before = deepcopy(source.entries)
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert message["text"] == native["payload"]["content"][0]["text"]
    assert _sources(message) == ["unknown"]
    assert source.entries == before
    if change == "unknown_source":
        values = []
        for marker in message["authorship"][0]["markers"]:
            values.append(marker["value"])
        assert {"type": "future", "detail": [None, 1]} in values


def test_native_images_keep_candidate_prompt_and_image_ownership():
    source = _source()
    checked = 0
    session = codex.interpret_source(source)
    exported = export_session(session)
    for native in capture.response_items(source.entries, "message"):
        payload = native["payload"]
        if "user.image" not in payload.get(META, {}).get("content_item_kinds", []):
            continue
        message = _message(session, native)
        sources = _sources(message)
        image_count = len(message["images"])
        assert sources == ["candidate"] * (image_count * 2 + 1)
        prompt = message["authorship"][-1]
        assert (
            message["text"][prompt["start"] : prompt["end"]]
            == payload["content"][-1]["text"]
        )
        assert exported["events"][message["i"]]["images"] == message["images"]
        checked += 1
    assert checked == 2


def test_native_skill_expansion_is_harness_but_its_selection_is_candidate():
    source = _source()
    session = codex.interpret_source(source)
    selections = []
    expansions = []
    for native in capture.response_items(source.entries, "message"):
        payload = native["payload"]
        kinds = payload.get(META, {}).get("content_item_kinds", [])
        if kinds == ["skills.selected_skill_instructions"]:
            message = _message(session, native)
            assert _sources(message) == ["harness"]
            assert message["skill_invocation"]["name"]
            expansions.append(message)
        elif kinds == ["user.text"] and payload["content"][0]["text"].startswith(
            "$capture-greeting"
        ):
            message = _message(session, native)
            assert _sources(message) == ["candidate"]
            selections.append(message)
    assert len(expansions) == len(selections) == 2


def test_native_user_shell_command_and_outcome_remain_candidate_activity():
    source = _source()
    native = _native_kind(source, "shell.user_command")
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert message["text"] == native["payload"]["content"][0]["text"]
    assert _sources(message) == ["candidate"]
    span = message["authorship"][0]
    assert (span["start"], span["end"]) == (0, len(message["text"]))
    assert message["shell_command"] in message["text"]
    assert span["ts"] == native["timestamp"]
    exported = export_session(session)
    assert exported["events"][message["i"]]["authorship"] == message["authorship"]


@pytest.mark.parametrize("child", [False, True])
def test_native_agent_deliveries_are_other_agent_without_inherited_human_inputs(child):
    path = capture.CHILD_TRANSCRIPT if child else capture.TRANSCRIPT
    source = codex.read_source(path)
    session = codex.interpret_source(source)
    messages = capture.response_items(source.entries, "agent_message")
    native = messages[-1]
    message = _message(session, native)
    assert _sources(message) == ["other_agent"]
    assert message["text"] == native["payload"]["content"][0]["text"]
    assert message["ts"] == native["timestamp"]
    markers = message["authorship"][0]["markers"]
    assert {"field": "payload.author", "value": native["payload"]["author"]} in markers
    assert {
        "field": "payload.recipient",
        "value": native["payload"]["recipient"],
    } in markers
    if child:
        assert session["parent_session_id"]
        for event in session["events"]:
            if "authorship" in event:
                assert "candidate" not in _sources(event)


@pytest.mark.parametrize("evidence", ["kind_only", "provenance_only"])
def test_recorded_evidence_supplies_authorship_independently_of_role(evidence):
    source = _source()
    native = capture.human_message(source.entries)
    key = "content_item_metadata" if evidence == "kind_only" else "content_item_kinds"
    native["payload"][META].pop(key)
    native["payload"]["role"] = "assistant"
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert message["role"] == "assistant"
    assert _sources(message) == ["candidate"]


@pytest.mark.parametrize(
    "content",
    [
        "plain text",
        ["plain text"],
        [{"output_text": "plain text"}],
        [{"text": ""}, {"text": "plain text"}],
    ],
)
def test_text_representation_boundaries_preserve_ranges_without_evidence(content):
    # Controlled compatibility boundaries; these do not claim new captured shapes.
    text = (
        "\nplain text"
        if isinstance(content, list) and len(content) == 2
        else "plain text"
    )
    spans = message_authorship({"content": content}, text, None, "response_item")
    for span in spans:
        assert span["source"] == "unknown"
        assert "ts" not in span
    assert spans[0]["start"] == (1 if text.startswith("\n") else 0)
    assert spans[-1]["end"] == len(text)


@pytest.mark.parametrize("value", [[], [False], [{"future": None}], "invalid"])
def test_malformed_provenance_never_becomes_candidate(value):
    source = _source()
    native = capture.human_message(source.entries)
    native["payload"][META]["content_item_metadata"] = value
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert _sources(message) == ["unknown"]


def test_all_native_human_text_preserves_words_and_candidate_authorship():
    source = _source()
    session = codex.interpret_source(source)
    checked = 0
    literal_xml_seen = False
    for native in capture.response_items(source.entries, "message"):
        payload = native["payload"]
        if payload.get(META, {}).get("content_item_kinds") != ["user.text"]:
            continue
        message = _message(session, native)
        native_text = payload["content"][0]["text"]
        assert message["text"] == native_text
        assert _sources(message) == ["candidate"]
        if "<bash-stdout>" in native_text:
            literal_xml_seen = True
        checked += 1
    assert checked > 0
    assert literal_xml_seen, "Native literal-markup input is required"


@pytest.mark.parametrize("same_input", [True, False])
def test_legacy_image_views_preserve_ownership_and_content_timestamps(same_input):
    source = _source()
    native = capture.image_message(source.entries)
    source.entries = [native]
    original = codex.interpret_source(source)["events"][0]
    event_text = original["text"] if same_input else "A distinct input"
    source.entries.append(
        {
            "type": "event_msg",
            "timestamp": "event-time",
            "payload": {"type": "user_message", "message": event_text},
        }
    )
    actual = codex.interpret_source(source)["events"]
    assert actual[0]["images"] == original["images"]
    assert actual[0]["text"] == original["text"]
    assert _sources(actual[0]) == _sources(original)
    for span in actual[0]["authorship"]:
        assert span["ts"] == native["timestamp"]
    if same_input:
        assert len(actual) == 1
        assert actual[0]["ts"] == "event-time"
        assert actual[0]["id"] == native["payload"]["id"]
    else:
        assert len(actual) == 2
        assert actual[0]["ts"] == native["timestamp"]
        assert actual[1]["ts"] == "event-time"
        assert "images" not in actual[1]
        assert actual[1]["text"] == event_text
        assert _sources(actual[1]) == ["unknown"]


def test_missing_shell_structure_preserves_unknown_text():
    source = _source()
    native = _native_kind(source, "shell.user_command")
    # Deliberately break only the native command delimiter.
    payload = native["payload"]
    text = payload["content"][0]["text"].replace("<command>", "<future_command>")
    payload["content"][0]["text"] = text
    source.entries = [native]
    message = codex.interpret_source(source)["events"][0]
    assert message["text"] == text
    assert _sources(message) == ["unknown"]


def test_shell_lookalike_in_recorded_human_input_stays_candidate():
    source = _source()
    shell = _native_kind(source, "shell.user_command")
    human = capture.human_message(source.entries)
    # The human can paste a shell transcript literally. Keep human provenance.
    text = shell["payload"]["content"][0]["text"]
    human["payload"]["content"][0]["text"] = text
    source.entries = [human]
    message = codex.interpret_source(source)["events"][0]
    assert message["text"] == text
    assert _sources(message) == ["candidate"]


@pytest.mark.parametrize(
    "change", ["missing_author", "empty_recipient", "same_actor", "unknown_namespace"]
)
def test_incomplete_agent_evidence_remains_unknown(change):
    source = _source()
    native = capture.response_items(source.entries, "agent_message")[0]
    payload = native["payload"]
    if change == "missing_author":
        payload.pop("author")
    elif change == "empty_recipient":
        payload["recipient"] = ""
    elif change == "same_actor":
        payload["recipient"] = payload["author"]
    else:
        payload[META]["content_item_metadata"][0]["provenance"]["namespace"] = "future"
    source.entries = [native]
    message = codex.interpret_source(source)["events"][0]
    assert message["text"] == payload["content"][0]["text"]
    assert _sources(message) == ["unknown"]


@pytest.mark.parametrize("child", [False, True])
def test_complete_native_capture_has_no_unresolved_text_activity(child):
    path = capture.CHILD_TRANSCRIPT if child else capture.TRANSCRIPT
    source = codex.read_source(path)
    session = codex.interpret_source(source)
    exported = export_session(session)
    unresolved = []
    for message in session["events"]:
        text = message.get("text") or ""
        if not text:
            continue
        spans = message.get("authorship", [])
        if not spans:
            unresolved.append((message.get("id"), "missing ranges"))
            continue
        offset = 0
        for span in spans:
            if span["source"] == "unknown":
                unresolved.append((message.get("id"), span["start"], span["end"]))
            # Only serialization whitespace may remain outside native ranges.
            assert not text[offset : span["start"]].strip()
            offset = span["end"]
        assert not text[offset:].strip()
        assert exported["events"][message["i"]]["authorship"] == spans
    assert unresolved == []


@pytest.mark.parametrize(
    "change",
    ["unknown_kind", "conflicting_provenance", "wrong_record_kind", "input_block"],
)
def test_primary_response_recognition_does_not_hide_conflicting_evidence(change):
    source = _source()
    for entry in capture.response_items(source.entries, "message"):
        if entry["payload"].get("role") == "assistant":
            payload = entry["payload"]
            break
    else:
        pytest.fail("Capture must supply a primary-assistant response")
    text = payload["content"][0]["text"]
    if change == "unknown_kind":
        payload[META]["content_item_kinds"] = ["future.output"]
    elif change == "conflicting_provenance":
        payload[META]["content_item_kinds"] = ["user.text"]
        payload[META]["content_item_metadata"] = [{"provenance": {"type": "harness"}}]
    elif change == "wrong_record_kind":
        payload["type"] = "future-message"
    else:
        payload["content"][0]["type"] = "input_text"
    spans = message_authorship(payload, text, entry["timestamp"], "response_item")
    assert len(spans) == 1
    assert spans[0]["source"] == "unknown"
    assert spans[0]["end"] == len(text)
