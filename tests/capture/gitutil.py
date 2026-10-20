import os
import subprocess

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Cand",
    "GIT_AUTHOR_EMAIL": "c@x.test",
    "GIT_COMMITTER_NAME": "Cand",
    "GIT_COMMITTER_EMAIL": "c@x.test",
}


def sh(cwd, *args):
    return subprocess.run(
        args, cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout
