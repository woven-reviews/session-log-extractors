#!/usr/bin/env python3
"""Hook entry point for session log capture (Claude Code, Codex, Copilot CLI).

Hook payload arrives as JSON on stdin; see src/session_log_extractors/capture. In a
starter repo this script sits next to a synced copy of the package, ``session_log_capture``.
"""

import sys
from pathlib import Path

# Never leave __pycache__ in the candidate's clone: it would show up in their git status.
sys.dont_write_bytecode = True


def load_cli():
    try:
        from session_log_extractors.capture import cli
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from session_log_capture import cli
    return cli


def run(argv, stdin):
    text = "" if stdin.isatty() else stdin.read()
    return load_cli().main(argv, text, Path(__file__).resolve())


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], sys.stdin))
