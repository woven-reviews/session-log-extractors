"""Type-policy enforcement rejects new, broadened, stale, or hidden exceptions."""

import json
import runpy
import subprocess
import sys
import tomllib
from collections import Counter
from copy import deepcopy
from pathlib import Path

import check_type_escapes as policy
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "pyproject.toml").read_text()
SOURCE = (
    'def f():\n    value: int = "wrong"  # type: ignore[assignment]\n    return value\n'
)


def _git(root, *arguments):
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Policy Test",
            "-c",
            "user.email=policy@example.invalid",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _entry(source=SOURCE):
    found, errors = policy.scan_source("example.py", source)
    assert not errors
    (key,) = found
    return {
        **key._asdict(),
        "reason": "Existing legacy boundary",
        "ticket": "QUAL-2518",
    }


def _inventory(root, entries):
    (root / policy.INVENTORY).write_text(json.dumps(entries))


@pytest.fixture
def repository(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    _inventory(tmp_path, [])
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial policy")
    return tmp_path


def _approve_legacy(repository, source=SOURCE):
    (repository / "example.py").write_text(source)
    _inventory(repository, [_entry(source)])
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Existing approved exception")


@pytest.mark.parametrize(
    "source",
    [
        'text = "# type: ignore"\n',
        '"""# mypy: ignore-errors\n# noqa: TID251\n"""\n',
        "x = 1  # noqa: C901\n",
        "# This comment explains noqa directives.\nx = 1\n",
    ],
)
def test_scanner_ignores_examples_in_strings_and_unrelated_lint_debt(source):
    assert policy.scan_source("example.py", source) == ([], [])


@pytest.mark.parametrize(
    "comment",
    [
        "# type: ignore",
        "# type: ignore[]",
        "# type: ignore[*]",
        "# type: ignore[unused-ignore]",
        "# type: ignore[ignore-without-code]",
        "# mypy: ignore-errors",
        "# mypy: disable-error-code=assignment",
        "# pyright: ignore",
        "# noqa",
        "# noqa: TID251",
        "# NoQA: tid251",
        "# ruff: NoQA: TID251",
        "# noqa: TID251 because this is old",
        "# noqa: TID",
        "# ruff: noqa: PGH003",
        "# noqa: RUF100",
        "# noqa: PGH004",
    ],
)
def test_scanner_rejects_escape_hatch_comments(comment):
    _, errors = policy.scan_source("example.py", f"x = 1  {comment}\n")
    assert errors
    assert errors[0].startswith("example.py:1:")


def test_file_level_ignore_is_rejected_even_with_error_codes():
    _, errors = policy.scan_source("example.py", "# type: ignore[assignment]\nx = 1\n")
    assert "must annotate a statement" in errors[0]


def test_identity_uses_symbol_statement_and_exact_normalized_codes():
    source = "class C:\n    async def f(self):\n        x = 1  # type: ignore[arg-type, assignment]\n"
    found, errors = policy.scan_source("example.py", source)
    assert not errors
    (key,) = found
    assert key.scope == "C.f"
    assert key.statement == "x = 1"
    assert key.codes == ("arg-type", "assignment")


@pytest.mark.parametrize("content", ["{}", "[1]", "[{}]", '[{"reason":"why"}]'])
def test_inventory_rejects_invalid_entries(content):
    with pytest.raises(ValueError, match="inventory|exception|require"):
        policy.load_inventory(content)


@pytest.mark.parametrize("codes", [None, [], [1], ["*"], ["unused-ignore"]])
def test_inventory_rejects_invalid_or_policy_disabling_codes(codes):
    entry = _entry()
    entry["codes"] = codes
    with pytest.raises(ValueError, match="type ignores"):
        policy.load_inventory(json.dumps([entry]))


def test_inventory_must_have_a_reason_and_ticket():
    entry = _entry()
    entry["ticket"] = ""
    with pytest.raises(ValueError, match="ticket"):
        policy.load_inventory(json.dumps([entry]))


def test_new_ignore_cannot_be_hidden_by_editing_inventory(repository):
    (repository / "example.py").write_text(SOURCE)
    errors = policy.check_policy(repository, "HEAD")
    assert any("unapproved type ignore" in error for error in errors)
    _inventory(repository, [_entry()])
    assert any(
        "may only shrink" in error for error in policy.check_policy(repository, "HEAD")
    )


def test_legacy_inventory_accepts_line_moves_but_requires_removal_with_code(repository):
    _approve_legacy(repository)
    (repository / "example.py").write_text("\n\n" + SOURCE)
    assert policy.check_policy(repository, "HEAD") == []
    (repository / "example.py").write_text(
        SOURCE.replace("  # type: ignore[assignment]", "")
    )
    assert any(
        "stale inventory entry" in error
        for error in policy.check_policy(repository, "HEAD")
    )
    _inventory(repository, [])
    assert policy.check_policy(repository, "HEAD") == []


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("[assignment]", "[assignment, arg-type]"),
        SOURCE.replace("def f", "def other"),
        SOURCE.replace('"wrong"', '"different"'),
    ],
)
def test_changed_scope_statement_or_codes_are_new_exceptions(repository, source):
    _approve_legacy(repository)
    (repository / "example.py").write_text(source)
    _inventory(repository, [_entry(source)])
    assert any(
        "may only shrink" in error for error in policy.check_policy(repository, "HEAD")
    )


def test_duplicate_suppressions_cannot_share_one_inventory_entry(repository):
    _approve_legacy(repository)
    line = SOURCE.splitlines()[1] + "\n"
    (repository / "example.py").write_text(SOURCE.replace(line, line + line))
    assert any(
        "unapproved type ignore" in error
        for error in policy.check_policy(repository, "HEAD")
    )


@pytest.mark.parametrize(
    "change",
    [
        ("mypy", "check_untyped_defs", False, "mypy must"),
        ("mypy", "warn_unused_ignores", False, "mypy must"),
        ("mypy", "enable_error_code", [], "mypy must"),
        ("mypy", "ignore_errors", True, "mypy suppressions"),
        ("mypy", "follow_imports", "skip", "mypy suppressions"),
        (
            "mypy",
            "overrides",
            [{"module": "legacy", "ignore_errors": True}],
            "mypy suppressions",
        ),
        ("ruff", "select", [], "Ruff type-policy"),
        ("ruff", "ignore", ["TID"], "Ruff type-policy"),
        ("ruff", "per-file-ignores", {"*.py": ["TID251"]}, "Ruff type-policy"),
        ("ruff", "extend-per-file-ignores", {"*.py": ["PGH003"]}, "Ruff type-policy"),
        ("ruff", "flake8-tidy-imports", {"banned-api": {}}, "Ruff must ban"),
    ],
)
def test_policy_settings_cannot_disable_enforcement(repository, monkeypatch, change):
    section, key, value, message = change
    settings = deepcopy(tomllib.loads(CONFIG))
    target = (
        settings["tool"]["mypy"]
        if section == "mypy"
        else settings["tool"]["ruff"]["lint"]
    )
    target[key] = value
    monkeypatch.setattr(policy.tomllib, "loads", lambda content: settings)
    assert any(message in error for error in policy._policy_settings(repository))


@pytest.mark.parametrize(
    "source",
    [
        "from typing import cast as trust\nx = trust(str, 1)\n",
        "import typing as t\nx = t.cast(str, 1)\n",
        "from typing_extensions import cast as trust\nx = trust(str, 1)\n",
        "from typing import no_type_check\n@no_type_check\ndef f():\n    pass\n",
        "import typing_extensions as t\n@t.no_type_check\ndef f():\n    pass\n",
    ],
)
def test_ruff_rejects_banned_apis_including_aliases(source):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "json",
            "-",
        ],
        input=source,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(item["code"] == "TID251" for item in json.loads(result.stdout))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('x: int = "wrong"  # type: ignore[assignment]\n', None),
        ("x = 1  # type: ignore[assignment]\n", "unused-ignore"),
        ('x: int = "wrong"  # type: ignore\n', "ignore-without-code"),
    ],
)
def test_mypy_requires_codes_and_rejects_stale_ignores(tmp_path, source, expected):
    path = tmp_path / "example.py"
    path.write_text(source)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "cache"),
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if expected is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert expected in result.stdout


def test_main_passes_and_reports_an_unapproved_ignore(repository, capsys):
    assert policy.main(["--compare-branch", "HEAD"], root=repository) == 0
    assert "Type escape policy passed" in capsys.readouterr().out
    (repository / "example.py").write_text(SOURCE)
    assert policy.main(["--compare-branch", "HEAD"], root=repository) == 1
    assert "unapproved type ignore" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["bad-ref", "invalid-source", "missing-inventory"])
def test_main_fails_closed_on_invalid_inputs(repository, capsys, failure):
    reference = "HEAD"
    if failure == "bad-ref":
        reference = "does-not-exist"
    elif failure == "invalid-source":
        (repository / "example.py").write_text("not valid Python !\n")
    else:
        (repository / policy.INVENTORY).unlink()
    assert policy.main(["--compare-branch", reference], root=repository) == 1
    assert capsys.readouterr().err


def test_cli_entrypoint(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["check_type_escapes.py", "--compare-branch", "HEAD"]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts/check_type_escapes.py"), run_name="__main__")
    assert exc.value.code == 0


def test_empty_inventory_contains_no_exceptions():
    assert policy.load_inventory((ROOT / policy.INVENTORY).read_text()) == Counter()
