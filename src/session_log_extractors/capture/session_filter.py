"""The privacy filter plug-in: what the capture hook must call before anything is saved.

The filter is not part of this package. A starter repo provides it as
``session_log_filter.py`` in the same directory as the other capture modules
(``scripts/session_log_capture/``). The hook loads it by path on every run.

Contract: the module defines four functions. Each takes the raw text a harness wrote
and returns the text that is safe to save. Raising any exception rejects the whole
file, which then is not saved.

    filter_claude(text: str) -> str          # a Claude Code transcript (JSONL)
    filter_codex(text: str) -> str           # a Codex rollout (JSONL)
    filter_copilot_events(text: str) -> str  # a Copilot events.jsonl
    filter_copilot_db(text: str) -> str      # JSON dump of one Copilot session's database rows

The plug-in is optional. Without the module, capture uses the built-in envelope mode
(``envelope``): the existing extractor's capped envelope is pushed, never the raw transcript.

Capture fails closed. If the module exists but lacks a function or raises, nothing is
pushed and the hook prints one warning line. There is never an unfiltered fallback.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

MODULE_FILE = "session_log_filter.py"
FUNCTIONS = (
    "filter_claude",
    "filter_codex",
    "filter_copilot_events",
    "filter_copilot_db",
)


class FilterError(Exception):
    """The filter is unavailable or refused a file; nothing may be saved."""


@dataclass
class SessionFilter:
    module: ModuleType

    def apply(self, name: str, text: str) -> str:
        """Run one filter function; any failure, or a non-text result, rejects the file."""
        try:
            result = getattr(self.module, name)(text)
        except Exception as err:
            raise FilterError(
                f"{MODULE_FILE} rejected the file ({type(err).__name__}: {err})"
            ) from err
        if not isinstance(result, str):
            raise FilterError(f"{MODULE_FILE}:{name} did not return text")
        return result


def load(directory: Path) -> SessionFilter:
    """Load ``session_log_filter.py`` from ``directory``; raise ``FilterError`` if it cannot be used."""
    path = directory / MODULE_FILE
    if not path.is_file():
        raise FilterError(f"no {MODULE_FILE} in {directory}")
    try:
        spec = importlib.util.spec_from_file_location("session_log_filter", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as err:
        raise FilterError(
            f"{MODULE_FILE} could not be loaded ({type(err).__name__}: {err})"
        ) from err
    missing = [n for n in FUNCTIONS if not callable(getattr(module, n, None))]
    if missing:
        raise FilterError(f"{MODULE_FILE} is missing {', '.join(missing)}")
    return SessionFilter(module)
