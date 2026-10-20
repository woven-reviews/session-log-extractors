"""Command line for the capture hook.

Hook mode reads one harness hook payload from stdin, stages the filtered session, and
starts a background worker. It always exits 0: a capture problem must never block or fail
the candidate's agent session, so errors become a single warning line instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gitpush, locate, pusher

NOTICE = "Session logs for this assessment are saved to a session-logs branch of your repository."


def repo_root(script: Path, override: str | None) -> Path:
    """The clone this hook belongs to: ``--repo`` if given, else the checkout holding the script."""
    if override:
        return Path(override)
    return Path(
        gitpush.git(script.parent, "rev-parse", "--show-toplevel").decode().strip()
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="capture_session.py")
    parser.add_argument("--harness", choices=["claude", "codex", "copilot"])
    parser.add_argument(
        "--repo",
        help="clone to capture for (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--worker", action="store_true", help="run the background push worker"
    )
    parser.add_argument(
        "--notice",
        action="store_true",
        help="print the capture notice for a SessionStart hook",
    )
    return parser.parse_args(argv)


def _payload(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def capture(
    args: argparse.Namespace, stdin_text: str, script: Path, home: Path, root: Path
) -> None:
    warning = pusher.take_last_error(root)
    if warning:
        print(warning, file=sys.stderr)
    if pusher.is_disabled(root):
        return
    payload = _payload(stdin_text)
    harness = args.harness or locate.detect_harness(payload)
    snap = locate.build_snapshot(harness, payload, root, home) if harness else None
    if snap is None:
        return
    pusher.stage(root, snap)
    pusher.spawn_worker(root, script)


def main(
    argv: list[str], stdin_text: str, script: Path, home: Path | None = None
) -> int:
    """Entry point shared by every harness hook. Returns the process exit code (always 0)."""
    args = _parse(argv)
    if args.notice:
        print(json.dumps({"systemMessage": NOTICE}))
        return 0
    try:
        root = repo_root(script, args.repo)
        if args.worker:
            pusher.run_worker(root)
        else:
            capture(args, stdin_text, script, home or Path.home(), root)
    except Exception as err:
        # A hook must never fail the agent session, so every error becomes one warning line.
        print(f"woven-capture: skipped ({type(err).__name__}: {err})", file=sys.stderr)
    return 0
