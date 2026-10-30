#!/usr/bin/env python3
"""Sanitize a captured file with explicit rules; never modify the source file."""

import sys

from session_log_extractors.sanitize_capture_cli import main

if __name__ == "__main__":
    sys.exit(main())
