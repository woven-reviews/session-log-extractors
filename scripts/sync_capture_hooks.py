#!/usr/bin/env python3
"""Sync the session log capture hooks into a starter repo: sync_capture_hooks.py TARGET_REPO."""

import argparse
import sys
from pathlib import Path

from session_log_extractors.capture import sync


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="path to the starter repo checkout")
    args = parser.parse_args(argv)
    if not (args.target / ".git").exists():
        print(f"{args.target} is not a git checkout", file=sys.stderr)
        return 1
    source_root = Path(__file__).resolve().parent.parent
    changed = sync.sync(args.target, source_root)
    sync.remove_pycache(args.target)
    print("\n".join(changed) if changed else "already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
