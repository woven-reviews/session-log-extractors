"""The installed command delegates JSON export to the existing harness CLIs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.harness import Harness

_EXPORTERS: dict[str, Callable[[list[str]], int]] = {
    Harness.CLAUDE: claude.main,
    Harness.CODEX: codex.main,
    Harness.COPILOT: copilot.main,
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch only the command name; each exporter owns its own arguments."""
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="session-log")
    parser.add_argument("harness", choices=list(_EXPORTERS))
    if not args or args[0] not in _EXPORTERS:
        parser.parse_args(args)
    return _EXPORTERS[args[0]](args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
