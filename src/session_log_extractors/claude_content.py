"""Decode Claude native result content without applying export policy."""

from session_log_extractors.claude_protocol import TEXT_BLOCK
from session_log_extractors.json_types import JsonValue


def result_text(content: JsonValue) -> str:
    """Pass strings through and join supported text blocks with spaces."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == TEXT_BLOCK:
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return " ".join(parts)
