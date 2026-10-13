#!/usr/bin/env python3
"""Checks that fixture redaction keeps the native transcript shape."""

import json
from pathlib import Path

from generate_claude_fixtures import clean


def test_clean_preserves_records_blocks_and_ids():
    session_id = "12345678-1234-1234-1234-123456789abc"
    entries = [
        {"type": "system", "message": "init", "sessionId": session_id},
        {"type": "user", "message": {"content": []}, "toolUseResult": {"stdout": "ok"}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "private", "signature": "sig"},
            {"type": "text", "text": "/tmp/fixture-project"},
        ]}},
    ]
    rows = [json.loads(line) for line in clean(entries, Path("/tmp/fixture-project")).splitlines()]
    assert [row["type"] for row in rows] == ["system", "user", "assistant"]
    assert rows[0]["sessionId"] == session_id
    assert rows[1]["toolUseResult"] == {"stdout": "ok"}
    assert rows[2]["message"]["content"] == [
        {"type": "thinking", "thinking": "[redacted]", "signature": "sig"},
        {"type": "text", "text": "/workspace"},
    ]


def test_clean_rejects_unknown_thinking_shape():
    entries = [{"message": {"content": [{"type": "thinking", "text": "new format"}]}}]
    try:
        clean(entries, Path("/tmp/fixture-project"))
    except RuntimeError as exc:
        assert str(exc) == "unexpected thinking block format"
    else:
        raise AssertionError("unknown thinking format must be reviewed")


def test_clean_redacts_encoded_project_dir():
    entries = [{"text": "/home/x/.claude/projects/-srv-fixture-project/memory/"}]
    assert "-workspace/memory/" in clean(entries, Path("/srv/fixture-project"))


def test_clean_rejects_leftover_private_temp_path():
    entries = [{"text": "-private-var-folders-ab-T-other"}]
    try:
        clean(entries, Path("/tmp/fixture-project"))
    except RuntimeError as exc:
        assert str(exc) == "private path remains after sanitizing"
    else:
        raise AssertionError("encoded temp path must be rejected")


if __name__ == "__main__":
    test_clean_preserves_records_blocks_and_ids()
    test_clean_rejects_unknown_thinking_shape()
    test_clean_redacts_encoded_project_dir()
    test_clean_rejects_leftover_private_temp_path()
