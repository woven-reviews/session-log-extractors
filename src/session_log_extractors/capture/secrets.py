"""Fail-closed secret detection and size limits for captured session data.

The filter prefers losing a tool result to leaking a credential, so detection is
deliberately broad: known token shapes, ``KEY=value`` and ``password: value``
assignments, URL credentials, long high-entropy strings, and any reference to a
``.env`` file.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

RESULT_MARKER = "[tool result removed: possible secret]"
LINE_MARKER = "[line removed: possible secret]"

_ASSIGNMENT_WORDS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|auth"
)

_PATTERNS = [
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_\-]{16,}",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        r"\bAIza[0-9A-Za-z_\-]{30,}",
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        r"(?i)authorization:\s*bearer\s+\S+",
        r"://[^/\s:@]+:[^/\s@]{3,}@",
        r"(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)"
        r"[A-Z0-9_]*\s*=\s*\S+",
        rf"(?i)\b(?:{_ASSIGNMENT_WORDS})\b['\"]?\s*[:=]\s*['\"]?[^\s'\"]{{4,}}",
    )
]

_ENV_FILE = re.compile(r"""(?:^|[\s/\\"'=])\.env(?:\.[A-Za-z0-9_-]+)?(?=$|[\s"'])""")
_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_ENTROPY_FLOOR = 4.2

# Only system reminders are removed: they carry personal config and email. The other
# command wrapper tags are content the extractors read (slash commands, model switches).
_INJECTED_BLOCK = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)
_INJECTED_LOOSE = re.compile(r"</?system-reminder\b[^>]*/?>", re.IGNORECASE)


def _entropy(token: str) -> float:
    counts = Counter(token)
    total = len(token)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _mixed_classes(token: str) -> bool:
    """Real tokens mix digits with letters; long identifiers and paths rarely do."""
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def _has_high_entropy_token(text: str) -> bool:
    return any(
        _mixed_classes(m.group(0)) and _entropy(m.group(0)) >= _ENTROPY_FLOOR
        for m in _TOKEN.finditer(text)
    )


def looks_secret(text: str) -> bool:
    """True when ``text`` contains anything credential-shaped."""
    if any(pattern.search(text) for pattern in _PATTERNS):
        return True
    return _has_high_entropy_token(text)


def mentions_env_file(value: Any) -> bool:
    """True when a tool input (string or JSON-like) refers to a ``.env`` file."""
    if isinstance(value, str):
        return bool(_ENV_FILE.search(value))
    if isinstance(value, dict):
        return any(mentions_env_file(v) for v in value.values())
    if isinstance(value, list):
        return any(mentions_env_file(v) for v in value)
    return False


def clip_result(text: str, limit: int) -> str:
    """Keep the first ``limit`` bytes of a tool result, or drop it if it holds a secret.

    The secret check runs over the whole text, not the kept prefix, so a credential
    past the cut can't slip through by being "truncated away" in a later snapshot.
    """
    if looks_secret(text):
        return RESULT_MARKER
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    kept = raw[:limit].decode("utf-8", errors="ignore")
    return f"{kept}\n[truncated: {len(raw)} bytes total]"


def redact_lines(text: str) -> str:
    """Replace each credential-shaped line, leaving the rest of the text readable."""
    lines = text.split("\n")
    return "\n".join(LINE_MARKER if looks_secret(line) else line for line in lines)


def strip_injected_tags(text: str) -> str:
    """Remove harness-injected blocks (system reminders carry personal config and email)."""
    return _INJECTED_LOOSE.sub("", _INJECTED_BLOCK.sub("", text))
