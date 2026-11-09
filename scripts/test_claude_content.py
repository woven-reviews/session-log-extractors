"""Claude native result decoding keeps its harness-specific text semantics."""

import pytest

from session_log_extractors.claude_content import result_text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("unchanged", "unchanged"),
        (None, ""),
        ({"text": "not a block list"}, ""),
        (
            [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
            "first second",
        ),
        (
            [
                None,
                {"type": "image", "text": "ignored"},
                {"type": "text", "text": 7},
            ],
            "",
        ),
    ],
)
def test_native_result_text(content, expected):
    actual = result_text(content)
    assert actual == expected
