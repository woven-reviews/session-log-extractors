"""Shared export limits preserve native facts and account for each text copy."""

import json
from copy import deepcopy

import pytest

from session_log_extractors import session_log_envelope as envelope
from session_log_extractors.session_model import Session


def _session_with_results(records):
    # Controlled export-boundary records isolate text policy from native parsing.
    return Session(
        harness="test",
        session_id="policy",
        events=[
            {
                "i": 0,
                "role": "assistant",
                "tool_results": records,
            }
        ],
    )


@pytest.mark.parametrize("size", [8191, 8192, 8193])
def test_default_result_boundary(size):
    session = _session_with_results([])
    session["events"][0]["tool_calls"] = [{"name": "read", "result": "x" * size}]
    exported = envelope.export_session(session)
    (call,) = exported["events"][0]["tool_calls"]
    assert exported["truncation"]["tool_result_max_bytes"] == 8192
    assert call["result"] == "x" * min(size, 8192)
    assert call["result_bytes"] == size
    assert call["result_truncated"] is (size > 8192)


@pytest.mark.parametrize("limit", [0, 4, 8192, -1, -20])
def test_unmatched_and_alternate_text_have_independent_accounting(limit):
    record = {
        "tool_call_id": None,
        "ts": None,
        "is_error": False,
        "content": "a€z",
        "native_result": "🙂" * 5000,
    }
    session = _session_with_results([record])
    original = deepcopy(session)
    exported = envelope.export_session(session, limit)
    (actual,) = exported["events"][0]["tool_results"]
    for field, size in [("content", 5), ("native_result", 20000)]:
        text = record[field]
        expected = text if limit < 0 else text.encode()[:limit].decode(errors="ignore")
        assert actual[field] == expected
        assert actual[field + "_bytes"] == size
        assert actual[field + "_truncated"] is (0 <= limit < size)
    assert actual["tool_call_id"] is None
    assert session == original
    unlimited = envelope.export_session(session, -1)
    assert (
        unlimited["events"][0]["tool_results"][0]["native_result"]
        == record["native_result"]
    )


@pytest.mark.parametrize(
    "value",
    [None, "", {"output": "x" * 100000, "decision": "rejected"}, ["x" * 100000]],
)
def test_result_states_and_structured_payloads_remain_complete(value):
    record = {
        "tool_call_id": None,
        "ts": None,
        "is_error": False,
        "content": value,
        "native_result": value,
    }
    session = _session_with_results([record])
    session["events"][0]["tool_calls"] = [
        {"name": "read", "result": value, "result_record": record}
    ]
    exported = envelope.export_session(session, 0)
    (actual,) = exported["events"][0]["tool_results"]
    (call,) = exported["events"][0]["tool_calls"]
    assert actual["content"] == value
    assert actual["native_result"] == value
    assert call["result"] == value
    assert call["result_record"] == actual
    if value == "":
        assert actual["content_bytes"] == 0
        assert actual["content_truncated"] is False
    else:
        assert "content_bytes" not in actual
        assert "native_result_bytes" not in actual


def test_missing_result_text_stays_absent():
    record = {"tool_call_id": None, "ts": None, "is_error": False}
    exported = envelope.export_session(_session_with_results([record]), 0)
    assert exported["events"][0]["tool_results"] == [record]


@pytest.mark.parametrize("limit", [2, -1])
def test_reexport_preserves_original_text_accounting(limit):
    record = {
        "tool_call_id": None,
        "ts": None,
        "is_error": False,
        "content": "retained",
        "content_bytes": 9000,
        "content_truncated": True,
        "native_result": "alternate",
        "native_result_bytes": 20000,
        "native_result_truncated": True,
    }
    exported = envelope.export_session(_session_with_results([record]), limit)
    (actual,) = exported["events"][0]["tool_results"]
    assert actual["content_bytes"] == 9000
    assert actual["native_result_bytes"] == 20000
    assert actual["content_truncated"] is True
    assert actual["native_result_truncated"] is True


@pytest.mark.parametrize("extra", [0, 1])
def test_size_warning_uses_complete_utf8_serialization(extra, capsys):
    payload = "€" + "x" * (envelope.SIZE_WARN_BYTES - 3 + extra)
    size = envelope.warn_if_oversized("JSON", payload)
    captured = capsys.readouterr()
    assert size == envelope.SIZE_WARN_BYTES + extra
    assert captured.out == ""
    assert bool(captured.err) is bool(extra)
    if extra:
        assert "--tool-result-bytes" in captured.err
        assert "--raw-tool-result-bytes" not in captured.err


def test_file_warning_counts_images_and_structured_content(tmp_path, capsys):
    session = _session_with_results(
        [
            {
                "tool_call_id": None,
                "ts": None,
                "is_error": False,
                "native_result": {"nested": "€" * 1000000},
            }
        ]
    )
    session["events"][0]["images"] = [
        envelope.image_from_bytes(b"x" * 4500000, "image/png")
    ]
    exported = envelope.export_session(session, 0)
    path = tmp_path / "session.json"
    size = envelope.write_envelope(path, exported)
    captured = capsys.readouterr()
    assert size == len(path.read_bytes())
    assert size > 8 * 1024 * 1024
    assert json.loads(path.read_text()) == exported
    assert "--tool-result-bytes" in captured.err
    assert captured.out == ""
