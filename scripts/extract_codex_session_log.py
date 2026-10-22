#!/usr/bin/env python3
"""Run the packaged Codex extractor with checkout-based output defaults."""

import sys
from pathlib import Path

from session_log_extractors import extract_codex_session_log

if __name__ == "__main__":
    extract_codex_session_log.PROJECT_ROOT = Path(__file__).resolve().parent.parent
    sys.exit(extract_codex_session_log.main())
