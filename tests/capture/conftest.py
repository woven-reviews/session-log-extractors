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
