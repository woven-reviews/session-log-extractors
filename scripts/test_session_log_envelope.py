#!/usr/bin/env python3
"""Asserts for the shared raw session-log envelope builder.

Run: python3 scripts/test_session_log_envelope.py
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

from session_log_envelope import (
    SCHEMA,
    build_envelope,
    image_from_base64,
    image_from_bytes,
    media_type_for_ext,
    raw_filename,
    tool_call,
    truncate_result,
    unavailable_image,
    user_event,
    write_envelope,
)

# 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


def test_raw_filename_prefixes_every_harness():
    # Unlike the markdown log, whose Claude variant is unprefixed, every raw
    # envelope carries its harness so the consumer never has to infer it.
    assert raw_filename("claude", "abc") == "claude_session_log_raw_abc.json"
    assert raw_filename("codex", "abc") == "codex_session_log_raw_abc.json"
    assert raw_filename("copilot", "abc") == "copilot_session_log_raw_abc.json"


def test_image_from_bytes_round_trips():
    raw = base64.b64decode(_PNG_B64)
    img = image_from_bytes(raw, "image/png")
    assert img["media_type"] == "image/png"
    assert img["bytes"] == len(raw)
    assert base64.b64decode(img["data"]) == raw


def test_image_from_base64_keeps_payload_and_reports_decoded_size():
    img = image_from_base64(_PNG_B64, "image/png")
    assert img["data"] == _PNG_B64  # no pointless decode/encode round trip
    assert img["bytes"] == len(base64.b64decode(_PNG_B64))


def test_image_from_base64_marks_undecodable_payload_unavailable():
    img = image_from_base64("%%%not base64%%%", "image/png")
    assert img["unavailable"] is True
    assert "data" not in img


def test_unavailable_image_records_the_ref():
    img = unavailable_image("/tmp/gone.png")
    assert img == {"unavailable": True, "ref": "/tmp/gone.png"}


def test_media_type_for_ext():
    assert media_type_for_ext("png") == "image/png"
    assert media_type_for_ext(".JPG") == "image/jpeg"
    # Unknown extensions return None rather than a guess: a wrong media type
    # renders as a broken image downstream.
    assert media_type_for_ext("img") is None
    assert media_type_for_ext(None) is None


def test_truncate_result_reports_original_size():
    out = truncate_result("A" * 9000, 100)
    assert out["result_truncated"] is True
    assert out["result_bytes"] == 9000  # the original, not the retained length
    assert len(out["result"]) == 100


def test_truncate_result_leaves_short_results_alone():
    out = truncate_result("short", 100)
    assert out == {"result": "short", "result_bytes": 5, "result_truncated": False}


def test_truncate_result_never_splits_a_multibyte_character():
    # A 3-byte character straddling the byte budget must not produce invalid
    # UTF-8 — the file has to stay parseable JSON.
    out = truncate_result("€" * 100, 100)
    assert out["result_truncated"] is True
    assert len(out["result"].encode("utf-8")) <= 100
    out["result"].encode("utf-8").decode("utf-8")  # raises if split mid-character


def test_truncate_result_negative_budget_means_no_cap():
    out = truncate_result("A" * 9000, -1)
    assert out["result_truncated"] is False
    assert len(out["result"]) == 9000


def test_tool_call_keeps_input_verbatim():
    call = tool_call("Read", {"file_path": "/a/b.py", "limit": 20}, "contents", 4096)
    assert call["name"] == "Read"
    assert call["input"] == {"file_path": "/a/b.py", "limit": 20}
    assert call["result"] == "contents"


def test_tool_call_coerces_unserializable_input():
    call = tool_call("Weird", {"obj": object()}, None)
    json.dumps(call)  # must not raise


def test_build_envelope_shape():
    env = build_envelope(
        harness="claude",
        session_id="s1",
        events=[user_event(0, "2026-08-01T00:00:00Z", "hi")],
        cwd="/work",
        started_at="2026-08-01T00:00:00Z",
        models=["claude-opus-4-8"],
        tool_result_max_bytes=4096,
    )
    assert env["schema"] == SCHEMA == "raw-session-log/1"
    assert env["harness"] == "claude"
    assert env["session_id"] == "s1"
    assert env["truncation"] == {"tool_result_max_bytes": 4096}
    assert env["events"][0]["role"] == "user"


def test_write_envelope_round_trips_through_json():
    env = build_envelope(
        harness="codex",
        session_id="s2",
        events=[
            user_event(0, None, "see this", [image_from_base64(_PNG_B64, "image/png")])
        ],
    )
    out = Path(tempfile.mkdtemp()) / raw_filename("codex", "s2")
    size = write_envelope(out, env)

    assert size == len(out.read_bytes())
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded == env
    assert base64.b64decode(
        reloaded["events"][0]["images"][0]["data"]
    ) == base64.b64decode(_PNG_B64)


def test_user_event_omits_images_key_when_there_are_none():
    assert "images" not in user_event(0, None, "text")
    assert "images" in user_event(0, None, "text", [unavailable_image("x")])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
