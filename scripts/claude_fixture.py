"""The explicitly selected current Claude capture and its provenance references.

Selections return fresh records from the complete capture, never constructed
transcripts. Tests may make documented, targeted mutations for uncaptured edges.
"""

import json
from pathlib import Path

from session_log_extractors.claude_sources import read_source

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
_MANIFEST = json.loads((ROOT / "manifest.json").read_text())
TRANSCRIPT = ROOT / _MANIFEST["main_session"]["transcript"]
SUBAGENT = ROOT / _MANIFEST["subagents"][0]["transcript"]


def native_entries(*behaviors):
    """Select unchanged records by the coverage ledger's native line/ID evidence."""
    if not behaviors:
        return read_source(TRANSCRIPT).entries
    coverage = json.loads((ROOT / "coverage.json").read_text())
    indexed = {item["behavior"]: item for item in coverage["behaviors"]}
    lines = TRANSCRIPT.read_text().splitlines()
    selected = {}
    for behavior in behaviors:
        evidence = indexed[behavior]
        assert evidence["status"] == "captured", behavior
        assert evidence["native_records"], behavior
        for reference in evidence["native_records"]:
            assert ROOT / reference["file"] == TRANSCRIPT
            entry = json.loads(lines[reference["line"] - 1])
            assert entry["uuid"] == reference["message_id"]
            selected[reference["line"]] = entry
    return [selected[line] for line in sorted(selected)]


def tool_pair(behavior="multibyte_result_over_10kb"):
    """A captured call/result pair; use recorded IDs when relating the records."""
    assistant, result = native_entries(behavior)
    return assistant, result
