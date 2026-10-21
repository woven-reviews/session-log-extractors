"""The built-in capture mode: run the existing extractor and keep only its envelope.

Without a ``session_log_filter.py`` plug-in, a captured session is pushed as the
``raw-session-log/1`` envelope the starter repo's own extractor writes, never as the raw
harness transcript. That is the file a candidate uploads by hand today: tool results are
capped at 4 KB and the original size is recorded.

The extractors ship beside the hook (``scripts/extract_*_session_log.py``). The hook runs
them in a throwaway directory at push time, not inside the hook itself, so a large
transcript never eats the hook's time budget. Extractors that write a Markdown log beside
the envelope are supported; only the envelope is read, and the Markdown is discarded.

Capture fails closed: an extractor that is missing, fails, times out, or writes something
that is not an envelope for this harness raises ``EnvelopeError``, and nothing is saved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "raw-session-log/1"
TIMEOUT_SECONDS = 120
SCRIPTS = {
    "claude": "extract_claude_session_log.py",
    "codex": "extract_codex_session_log.py",
    "copilot": "extract_copilot_session_log.py",
}


class EnvelopeError(Exception):
    """The extractor is unavailable or produced no valid envelope; nothing may be saved."""


def valid_envelope(text: str, harness: str | None = None) -> dict[str, object] | None:
    """The parsed envelope if ``text`` is a ``raw-session-log/1`` object (for ``harness``, if given)."""
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    if not isinstance(loaded, dict) or loaded.get("schema") != SCHEMA:
        return None
    if harness and loaded.get("harness") not in (None, harness):
        return None
    return loaded


@dataclass
class EnvelopeBuilder:
    """Builds envelopes with the extractors in ``scripts``.

    ``deferred`` builders answer every request with an empty placeholder: the hook only
    needs to record that a session changed, and the worker builds the real envelope.
    """

    scripts: Path
    deferred: bool = False

    def claude(self, transcript: Path) -> str:
        return self._build("claude", ["--transcript", str(transcript)])

    def codex(self, transcript: Path) -> str:
        return self._build("codex", ["--transcript", str(transcript)])

    def copilot(self, session_id: str, home: Path, copilot_home: Path) -> str:
        args = [session_id, "--db", str(copilot_home / "session-store.db")]
        env = {"HOME": str(home), "COPILOT_HOME": str(copilot_home)}
        return self._build("copilot", args, env)

    def _build(
        self, harness: str, args: list[str], env: dict[str, str] | None = None
    ) -> str:
        if self.deferred:
            return ""
        script = self.scripts / SCRIPTS[harness]
        if not script.is_file():
            raise EnvelopeError(f"{script.name} is missing")
        with tempfile.TemporaryDirectory() as tmp:
            self._run(script, [*args, "--output", f"{tmp}/log.md"], env)
            return _read_envelope(Path(tmp), harness)

    @staticmethod
    def _run(script: Path, args: list[str], env: dict[str, str] | None) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, "-B", str(script), *args],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                env={**os.environ, **(env or {})},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise EnvelopeError(
                f"{script.name} could not run ({type(err).__name__})"
            ) from err
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [
                "failed"
            ]
            raise EnvelopeError(f"{script.name} failed: {reason[0][:200]}")


def _read_envelope(out: Path, harness: str) -> str:
    """The one envelope an extractor wrote under ``out``; Markdown and other files are ignored."""
    found = []
    for path in sorted(out.iterdir()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if valid_envelope(text, harness):
            found.append(text)
    if len(found) != 1:
        raise EnvelopeError(
            f"the {harness} extractor produced {len(found)} envelopes, expected 1"
        )
    return found[0]
