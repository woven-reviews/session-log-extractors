"""Offline Claude fixture paths shared by tests; capture uses the agent workflow."""

from pathlib import Path

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
NATIVE = ROOT / "native"
(TRANSCRIPT,) = NATIVE.glob("*.jsonl")
(CHILD_TRANSCRIPT,) = (TRANSCRIPT.with_suffix("") / "subagents").glob("*.jsonl")
ASSETS = ROOT / "assets"
