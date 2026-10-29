"""Codex authorship uses recorded block evidence, independently of role."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors.codex_authorship import message_authorship
from session_log_extractors.session_log_envelope import export_session

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
META = "internal_chat_message_metadata_passthrough"


def _source(name="slash.jsonl"):
    return codex.read_source(FIXTURES / name)


def _typed_input(source):
    return next(
        e
        for e in source.entries
        if "user.text"
        in e.get("payload", {}).get(META, {}).get("content_item_kinds", [])
    )


def _message(session, native):
    return next(e for e in session["events"] if e.get("id") == native["payload"]["id"])


def test_native_candidate_and_injected_blocks_reach_production_json(capsys):
    source = _source()
    native = _typed_input(source)
    session = codex.interpret_source(source)
    candidate = _message(session, native)
    assert candidate["text"] == "/review fixture"
    assert candidate["turn_id"] == native["payload"][META]["turn_id"]
    assert [r["source"] for r in candidate["authorship"]] == ["candidate"]
    injected = next(
        e for e in session["events"] if e["role"] == "user" and e != candidate
    )
    assert [r["source"] for r in injected["authorship"]] == ["harness", "harness"]
    assert injected["text"].startswith("# AGENTS.md instructions")
    assert injected["ts"]
    assert all(
        r["ts"] == injected["ts"] and r["markers"] for r in injected["authorship"]
    )
    original = deepcopy(session)
    assert (
        codex.main(["--transcript", str(FIXTURES / "slash.jsonl"), "--output", "-"])
        == 0
    )
    import_output = capsys.readouterr().out
    assert '"candidate"' in import_output and '"harness"' in import_output
    assert export_session(session)["events"] == json.loads(import_output)["events"]
    assert session == original


def test_mixed_native_blocks_keep_codepoint_offsets_and_recorded_markers():
    source = _source()
    native = _typed_input(source)
    context = next(
        e
        for e in source.entries
        if "environments.environment_context"
        in e.get("payload", {}).get(META, {}).get("content_item_kinds", [])
    )
    # Combine captured blocks into one message, retaining their aligned native evidence.
    payload = native["payload"]
    payload["content"][0]["text"] = "Hi 🙂"
    payload["content"].append(deepcopy(context["payload"]["content"][1]))
    for key in ["content_item_kinds", "content_item_metadata"]:
        payload[META][key].append(deepcopy(context["payload"][META][key][1]))
    message = _message(codex.interpret_source(source), native)
    first, second = message["authorship"]
    assert (first["start"], first["end"], first["source"]) == (0, 4, "candidate")
    assert second["start"] == 5 and second["end"] == len(message["text"])
    assert second["source"] == "harness"
    assert message["text"][:4] == "Hi 🙂"
    assert message["text"][5:] == payload["content"][1]["text"]
    assert {
        "field": f"payload.{META}.content_item_kinds[1]",
        "value": "environments.environment_context",
    } in second["markers"]
    assert {
        "field": f"payload.{META}.content_item_metadata[1].provenance",
        "value": {"type": "harness", "hook": False},
    } in second["markers"]


@pytest.mark.parametrize(
    "change", ["missing", "unknown_kind", "unknown_source", "misaligned", "conflict"]
)
def test_unsupported_evidence_stays_unknown_and_preserved(change):
    source = _source()
    native = _typed_input(source)
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
    message = _message(codex.interpret_source(source), native)
    assert message["text"] == "/review fixture"
    assert message["authorship"][0]["source"] == "unknown"
    assert source.entries == before
    if change == "unknown_source":
        assert any(
            m["value"] == {"type": "future", "detail": [None, 1]}
            for m in message["authorship"][0]["markers"]
        )


def test_native_image_delimiters_are_not_candidate_words():
    source = _source("image.jsonl")
    native = _typed_input(source)
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert [r["source"] for r in message["authorship"]] == [
        "unknown",
        "unknown",
        "candidate",
    ]
    assert [message["text"][r["start"] : r["end"]] for r in message["authorship"]][
        -1
    ] == "Describe the attached 64-by-64 test image briefly."
    assert message["images"]
    assert (
        export_session(session)["events"][message["i"]]["images"] == message["images"]
    )


@pytest.mark.parametrize("evidence", ["kind_only", "provenance_only"])
def test_recorded_evidence_supplies_authorship_independently_of_role(evidence):
    source = _source()
    native = _typed_input(source)
    # Older captures supply kinds only. Explicit provenance can also stand alone.
    key = "content_item_metadata" if evidence == "kind_only" else "content_item_kinds"
    native["payload"][META].pop(key)
    native["payload"]["role"] = "assistant"
    message = _message(codex.interpret_source(source), native)
    assert message["role"] == "assistant"
    assert message["authorship"][0]["source"] == "candidate"


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
    # These non-native content representations exercise compatibility/malformed
    # input boundaries, not additional claims about the captured schema.
    text = (
        "\nplain text"
        if isinstance(content, list) and len(content) == 2
        else "plain text"
    )
    spans = message_authorship({"content": content}, text, None, "response_item")
    assert all(r["source"] == "unknown" and "ts" not in r for r in spans)
    assert spans[0]["start"] == (1 if text.startswith("\n") else 0)
    assert spans[-1]["end"] == len(text)


@pytest.mark.parametrize("value", [[], [False], [{"future": None}], "invalid"])
def test_malformed_provenance_never_becomes_candidate(value):
    source = _source()
    native = _typed_input(source)
    native["payload"][META]["content_item_metadata"] = value
    message = _message(codex.interpret_source(source), native)
    assert message["authorship"][0]["source"] == "unknown"
