"""Recorded invocation occurrences, without external definition lookup."""

from copy import deepcopy

import pytest
from codex_fixture import TRANSCRIPT

from session_log_extractors import codex_protocol
from session_log_extractors.codex_sources import CodexSource, load_entries
from session_log_extractors.extract_codex_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _user(text, *, turn_id=None, selected=False):
    kinds = (
        [codex_protocol.SELECTED_SKILL_INSTRUCTIONS]
        if selected
        else [codex_protocol.USER_TEXT]
    )
    return {
        "type": codex_protocol.RESPONSE_ITEM,
        "payload": {
            "type": codex_protocol.MESSAGE,
            "role": "user",
            "content": [{"type": codex_protocol.INPUT_TEXT, "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn_id,
                "content_item_kinds": kinds,
            },
        },
    }


def _export(entries, limit=0):
    session = interpret_source(CodexSource(TRANSCRIPT, entries))
    assert session is not None
    before = deepcopy(session)
    envelope = export_session(session, limit)
    assert session == before
    return envelope


def _invocations(envelope):
    invocations = []
    for message in envelope["events"]:
        invocations.extend(message.get("invocations", []))
        for call in message.get("tool_calls") or []:
            invocations.extend(call.get("invocations", []))
    return invocations


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_repeated_selections_keep_full_definitions_and_distinct_occurrences(
    limit,
):
    entries = load_entries(TRANSCRIPT)
    definitions = []
    for entry in entries:
        payload = entry.get("payload", {})
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if metadata.get("content_item_kinds") == [
            codex_protocol.SELECTED_SKILL_INSTRUCTIONS
        ]:
            definitions.append(payload["content"][0]["text"])
    assert len(definitions) == 2, "Both recorded skill selections are required"

    envelope = _export(entries, limit)

    invocations = _invocations(envelope)
    assert len(invocations) == 2
    assert invocations[0]["ref"] != invocations[1]["ref"]
    for invocation, definition in zip(invocations, definitions, strict=True):
        assert invocation["kind"] == "skill"
        assert invocation["name"]
        assert invocation["definitions"][0]["text"] == definition
        assert invocation["definitions"][0]["path"] in definition


def test_exact_legacy_commands_and_arguments_survive_export():
    entries = [
        _user("/review   café 日本語  "),
        _user(
            "<command-name>/review</command-name><command-args>  two\nlines  </command-args>"
        ),
        _user("/review"),
    ]

    envelope = _export(entries)

    invocations = _invocations(envelope)
    assert len(invocations) == 3
    assert invocations[0]["slash_text"] == "/review   café 日本語  "
    assert invocations[0]["arguments"] == "café 日本語  "
    assert invocations[1]["arguments"] == "  two\nlines  "
    assert invocations[2]["arguments"] == ""
    for invocation in invocations:
        assert invocation["kind"] == "command"
        assert invocation["name"] == "/review"


def test_skill_link_requires_an_invocation_and_keeps_exact_syntax():
    link = "[$demo](plugin://package/skills/demo/SKILL.md)"
    entries = [
        _user(link),
        _user("$demo " + link + " with café"),
        _user("The path /tmp/demo/SKILL.md is only a mention."),
        _user(
            "<skill><name>demo</name><path>/tmp/demo/SKILL.md</path>Unlinked definition</skill>"
        ),
    ]

    envelope = _export(entries)

    (invocation,) = _invocations(envelope)
    assert invocation["name"] == "demo"
    assert invocation["kind"] == "skill"
    assert invocation["skill_links"] == [
        {"syntax": link, "target": "plugin://package/skills/demo/SKILL.md"}
    ]
    assert "definitions" not in invocation
    assert (
        envelope["events"][-1]["text"] == entries[-1]["payload"]["content"][0]["text"]
    )


def test_selected_expansion_without_selection_is_still_evidenced_but_not_guessed():
    definition = "<skill>\n<name>demo</name>\n<path>/tmp/demo/SKILL.md</path>\nFull café definition\n</skill>"
    entries = [
        _user("$demo", turn_id="one"),
        _user(definition, turn_id="two", selected=True),
    ]

    invocations = _invocations(_export(entries))

    assert len(invocations) == 2
    assert "definitions" not in invocations[0]
    assert invocations[1]["definitions"] == [
        {"text": definition, "path": "/tmp/demo/SKILL.md"}
    ]


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_known_skill_tool_keeps_arguments_and_complete_recorded_definitions(limit):
    definition = "Full café definition " * 300
    entries = [
        {
            "type": codex_protocol.RESPONSE_ITEM,
            "payload": {
                "type": codex_protocol.FUNCTION_CALL,
                "name": "mcp__skills__read",
                "call_id": "skill-call",
                "arguments": {
                    "package": "demo",
                    "args": " exact  café ",
                    "definition": "Recorded input definition",
                    "extra": [1, {"x": True}],
                },
            },
        },
        {
            "type": codex_protocol.RESPONSE_ITEM,
            "payload": {
                "type": codex_protocol.FUNCTION_CALL_OUTPUT,
                "call_id": "skill-call",
                "output": definition,
            },
        },
    ]

    envelope = _export(entries, limit)

    (invocation,) = _invocations(envelope)
    assert invocation["name"] == "demo"
    assert invocation["arguments"] == " exact  café "
    assert invocation["definitions"] == [
        {"text": "Recorded input definition"},
        {"text": definition},
    ]
    assert invocation["activity"]["tool_call_index"] == 0
    assert envelope["events"][0]["tool_calls"][0]["input"]["extra"] == [1, {"x": True}]


def test_failed_skill_read_does_not_turn_error_output_into_a_definition():
    entries = [
        {
            "type": codex_protocol.RESPONSE_ITEM,
            "payload": {
                "type": codex_protocol.FUNCTION_CALL,
                "name": codex_protocol.SKILLS_READ,
                "call_id": "skill-call",
                "arguments": {},
            },
        },
        {
            "type": codex_protocol.RESPONSE_ITEM,
            "payload": {
                "type": codex_protocol.FUNCTION_CALL_OUTPUT,
                "call_id": "skill-call",
                "output": "Cannot read",
                "is_error": True,
            },
        },
    ]

    (invocation,) = _invocations(_export(entries))

    assert invocation["name"] is None
    assert "definitions" not in invocation


def test_expansions_without_turn_ids_stay_separate_from_unproven_requests():
    definition = (
        "<skill><name>demo</name><path>/recorded/SKILL.md</path>Use $nested</skill>"
    )

    invocations = _invocations(
        _export([_user("$demo"), _user(definition, selected=True)])
    )

    assert len(invocations) == 2
    assert invocations[0]["name"] == "demo"
    assert invocations[1]["name"] == "demo"
    assert invocations[1]["definitions"][0]["text"] == definition


def test_unlinked_definitions_do_not_invoke_tokens_in_their_bodies():
    text = "<skill><name>demo</name><path>/recorded/SKILL.md</path>Use $nested</skill>"

    envelope = _export([_user(text)])

    assert _invocations(envelope) == []
    assert envelope["events"][0]["text"] == text


def test_repeated_requests_in_one_turn_do_not_guess_an_expansion_owner():
    definition = (
        "<skill><name>demo</name><path>/recorded/SKILL.md</path>Full definition</skill>"
    )
    entries = [
        _user("$demo $demo", turn_id="one"),
        _user(definition, turn_id="one", selected=True),
    ]

    invocations = _invocations(_export(entries))

    assert len(invocations) == 3
    assert invocations[0]["ref"] != invocations[1]["ref"]
    assert "definitions" not in invocations[0]
    assert "definitions" not in invocations[1]
    assert invocations[2]["definitions"][0]["text"] == definition


def test_harness_instructions_do_not_turn_embedded_skill_tokens_into_invocations():
    # Harness instructions use the native user role too; retain their content.
    entry = _user("Use $example in documentation under $CODEX_HOME.")
    entry["payload"]["internal_chat_message_metadata_passthrough"][
        "content_item_kinds"
    ] = [codex_protocol.AGENTS_MD_INSTRUCTIONS]

    envelope = _export([entry])

    assert _invocations(envelope) == []
    assert envelope["events"][0]["text"] == entry["payload"]["content"][0]["text"]


def test_mixed_harness_and_candidate_text_only_invokes_the_candidate_token():
    entry = _user("instructions mention $example")
    entry["payload"]["content"].append({"type": "input_text", "text": "$requested"})
    entry["payload"]["internal_chat_message_metadata_passthrough"][
        "content_item_kinds"
    ] = [codex_protocol.AGENTS_MD_INSTRUCTIONS, codex_protocol.USER_TEXT]

    (invocation,) = _invocations(_export([entry]))

    assert invocation["name"] == "requested"
