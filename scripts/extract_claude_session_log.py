#!/usr/bin/env python3
"""Run the packaged Claude extractor with checkout-based output defaults."""

import sys
from pathlib import Path

from session_log_extractors import extract_claude_session_log

if __name__ == "__main__":
    extract_claude_session_log.PROJECT_ROOT = Path(__file__).resolve().parent.parent
    sys.exit(extract_claude_session_log.main())
