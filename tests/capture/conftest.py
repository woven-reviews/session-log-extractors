import pytest
from gitutil import sh


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(path))
    return path


@pytest.fixture
def clone(tmp_path, remote):
    path = tmp_path / "clone"
    sh(tmp_path, "git", "clone", "-q", str(remote), str(path))
    (path / "app.py").write_text("print('hi')\n")
    sh(path, "git", "add", "app.py")
    sh(path, "git", "commit", "-q", "-m", "init")
    sh(path, "git", "push", "-q", "origin", "main")
    # Work in progress the capture must never disturb: a staged edit, an unstaged edit, an untracked file.
    (path / "staged.txt").write_text("staged\n")
    sh(path, "git", "add", "staged.txt")
    (path / "app.py").write_text("print('edited')\n")
    (path / "untracked.txt").write_text("u\n")
    return path


FILTER_SOURCES = {
    # Keeps everything: for tests of plumbing that must not depend on filtering rules.
    "identity": """
def filter_claude(text):
    return text


def filter_codex(text):
    return text


def filter_copilot_events(text):
    return text


def filter_copilot_db(text):
    return text
""",
    # A trivial real filter: Claude keeps only user and assistant entries, and Copilot
    # events lose their transformed content.
    "text_only": """
import json


def _entries(text):
    for raw in text.splitlines():
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def filter_claude(text):
    return "".join(
        json.dumps(e) + "\\n" for e in _entries(text) if e.get("type") in ("user", "assistant")
    )


def filter_codex(text):
    return text


def filter_copilot_events(text):
    out = []
    for entry in _entries(text):
        if isinstance(entry.get("data"), dict):
            entry["data"].pop("transformedContent", None)
        out.append(json.dumps(entry) + "\\n")
    return "".join(out)


def filter_copilot_db(text):
    return text
""",
    "raising": """
def filter_claude(text):
    raise RuntimeError("cannot judge this file")


filter_codex = filter_copilot_events = filter_copilot_db = filter_claude
""",
    "incomplete": """
def filter_claude(text):
    return text
""",
    "not_text": """
def filter_claude(text):
    return None


filter_codex = filter_copilot_events = filter_copilot_db = filter_claude
""",
}


def write_filter(directory, kind="text_only"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session_log_filter.py").write_text(FILTER_SOURCES[kind])
    return directory


@pytest.fixture
def filter_dir(tmp_path):
    return write_filter(tmp_path / "filter-plugin")


@pytest.fixture
def flt(filter_dir):
    from session_log_extractors.capture import session_filter

    return session_filter.load(filter_dir)
