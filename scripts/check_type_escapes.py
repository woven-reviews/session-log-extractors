#!/usr/bin/env python3
"""Reject unapproved type ignores and growth of the approved exception inventory.

Tokenize comments so examples in strings are not mistaken for suppressions.
Identify exceptions by file, enclosing symbol, normalized statement, and exact
error codes rather than line numbers or counts alone. The inventory may shrink,
but cannot grow relative to the comparison branch, even if edited in the PR.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from pathlib import Path
from typing import NamedTuple

INVENTORY = "scripts/typecheck_exceptions.json"
PROTECTED_RULES = {"TID251", "PGH003", "PGH004", "RUF100"}
BANNED_APIS = {
    "typing.cast",
    "typing_extensions.cast",
    "typing.no_type_check",
    "typing_extensions.no_type_check",
}
IGNORE = re.compile(r"^#\s*type:\s*ignore(?:\s*\[([^]]*)\])?")


class IgnoreKey(NamedTuple):
    path: str
    scope: str
    statement: str
    codes: tuple[str, ...]


def _codes(values: object) -> tuple[str, ...]:
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) for value in values)
    ):
        raise ValueError("type ignores require explicit error codes")
    values = [value.strip() for value in values]
    if not all(re.fullmatch(r"[a-z][a-z0-9-]*", value) for value in values):
        raise ValueError("type ignores require explicit error codes")
    if set(values) & {"unused-ignore", "ignore-without-code"}:
        raise ValueError("type ignores cannot disable ignore-policy checks")
    return tuple(sorted(set(values)))


def _text(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"inventory entries require a nonempty {key}")
    return value


def load_inventory(content: str) -> Counter[IgnoreKey]:
    entries = json.loads(content)
    if not isinstance(entries, list):
        raise ValueError("the type exception inventory must be a list")
    result: Counter[IgnoreKey] = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each type exception must be an object")
        _text(entry, "reason")
        _text(entry, "ticket")
        result[
            IgnoreKey(
                _text(entry, "path"),
                _text(entry, "scope"),
                _text(entry, "statement"),
                _codes(entry.get("codes")),
            )
        ] += 1
    return result


def _key(path: str, tree: ast.Module, line: int, codes: tuple[str, ...]) -> IgnoreKey:
    statements = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    statement = min(
        statements,
        key=lambda node: (
            (node.end_lineno or node.lineno) - node.lineno,
            (node.end_col_offset or 0) - node.col_offset,
        ),
    )
    scopes = [
        node
        for node in statements
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    scopes.sort(key=lambda node: (node.lineno, -(node.end_lineno or node.lineno)))
    return IgnoreKey(
        path,
        ".".join(node.name for node in scopes) or "<module>",
        ast.unparse(statement),
        codes,
    )


def _protects_rule(code: str) -> bool:
    return any(rule.startswith(code) for rule in PROTECTED_RULES)


def _unsafe_comment(comment: str) -> bool:
    if re.match(r"#\s*(?:mypy:|pyright:.*ignore)", comment, re.IGNORECASE):
        return True
    noqa = re.search(
        r"#\s*(?:(?:ruff|flake8):\s*)?noqa\b(?:\s*:\s*([A-Z0-9, ]+))?",
        comment,
        re.IGNORECASE,
    )
    if noqa:
        return not noqa.group(1) or any(
            _protects_rule(code.upper())
            for code in re.findall(r"[A-Z]+[0-9]*", noqa.group(1), re.IGNORECASE)
        )
    return False


def scan_source(path: str, content: str) -> tuple[list[IgnoreKey], list[str]]:
    tree = ast.parse(content)
    lines = content.splitlines()
    ignores: list[IgnoreKey] = []
    errors: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(content).readline):
        if token.type != tokenize.COMMENT:
            continue
        location = f"{path}:{token.start[0]}"
        if _unsafe_comment(token.string):
            errors.append(f"{location}: forbidden type-policy suppression")
        match = IGNORE.match(token.string)
        if match is None:
            continue
        try:
            codes = _codes(match.group(1).split(",") if match.group(1) else [])
            if not lines[token.start[0] - 1][: token.start[1]].strip():
                raise ValueError("type ignores must annotate a statement")
            ignores.append(_key(path, tree, token.start[0], codes))
        except ValueError as exc:
            errors.append(f"{location}: {exc}")
    return ignores, errors


def audit(
    ignores: list[IgnoreKey],
    inventory: Counter[IgnoreKey],
    baseline: Counter[IgnoreKey],
) -> list[str]:
    errors = []
    if inventory - baseline:
        errors.append(
            "type exception inventory may only shrink; new or broadened entries are forbidden"
        )
    actual = Counter(ignores)
    for key in actual - inventory:
        errors.append(f"{key.path}:{key.scope}: unapproved type ignore {key.codes}")
    for key in inventory - actual:
        errors.append(f"{key.path}:{key.scope}: stale inventory entry must be removed")
    return errors


def _policy_settings(root: Path) -> list[str]:
    tool = tomllib.loads((root / "pyproject.toml").read_text())["tool"]
    mypy = tool["mypy"]
    errors = []
    if (
        not mypy.get("check_untyped_defs")
        or not mypy.get("warn_unused_ignores")
        or "ignore-without-code" not in mypy.get("enable_error_code", [])
    ):
        errors.append(
            "mypy must check untyped bodies, reject unused ignores, and require error codes"
        )
    if (
        any(
            mypy.get(key)
            for key in (
                "ignore_errors",
                "ignore_missing_imports",
                "disable_error_code",
                "exclude",
                "overrides",
            )
        )
        or mypy.get("follow_imports", "normal") != "normal"
    ):
        errors.append(
            "mypy suppressions, exclusions, and skipped imports are forbidden"
        )
    lint = tool["ruff"]["lint"]
    selected = lint.get("select", []) + lint.get("extend-select", [])
    ignored = lint.get("ignore", []) + lint.get("extend-ignore", [])
    ignored += [
        code for codes in lint.get("per-file-ignores", {}).values() for code in codes
    ]
    ignored += [
        code
        for codes in lint.get("extend-per-file-ignores", {}).values()
        for code in codes
    ]
    if not all(
        any(rule.startswith(code) for code in selected) for rule in PROTECTED_RULES
    ) or any(_protects_rule(code) for code in ignored):
        errors.append(
            "protected Ruff type-policy rules must remain enabled without ignores"
        )
    banned = lint.get("flake8-tidy-imports", {}).get("banned-api", {})
    if not BANNED_APIS <= banned.keys():
        errors.append("Ruff must ban casts and no_type_check APIs")
    return errors


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    ).stdout


def _baseline(root: Path, reference: str) -> Counter[IgnoreKey]:
    try:
        content = _git(root, "show", f"{reference}:{INVENTORY}")
    except subprocess.CalledProcessError:
        # A new inventory starts empty; a nonexistent reference remains an error.
        _git(root, "rev-parse", "--verify", reference)
        content = "[]"
    return load_inventory(content)


def check_policy(root: Path, reference: str) -> list[str]:
    inventory = load_inventory((root / INVENTORY).read_text())
    baseline = _baseline(root, reference)
    errors = _policy_settings(root)
    ignores = []
    paths = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        "*.py",
    ).split("\0")
    for path in sorted(set(paths) - {""}):
        found, failures = scan_source(path, (root / path).read_text())
        ignores.extend(found)
        errors.extend(failures)
    return errors + audit(ignores, inventory, baseline)


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-branch", default="origin/main")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1] if root is None else root
    try:
        errors = check_policy(root, args.compare_branch)
    except (
        OSError,
        ValueError,
        SyntaxError,
        tokenize.TokenError,
        subprocess.CalledProcessError,
    ) as exc:
        errors = [str(exc)]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Type escape policy passed; exception inventory has not grown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
