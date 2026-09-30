"""Structured protection rules: values inside JSON and TOML files.

A protected path governs a whole file. A structured rule governs one value
inside matching files, named by a JSON Pointer (RFC 6901), so a push needs an
approval only when it changes that value; any other edit to the same file is
ordinary content. Like changed paths, field changes are derived from Git
objects alone, commit by commit over the commits new to the repository, so
the pre-receive hook and ``approve prepare`` agree on them.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from . import git_objects
from .work import path_in_scopes

FORMATS = ("json", "toml")


class RuleError(ValueError):
    pass


class _Missing:
    """No value: the file or the pointed-to member does not exist."""

    def __repr__(self) -> str:
        return "<missing>"


class _Unreadable:
    """Content that cannot be parsed. Never equal to anything, itself
    included, so an unparseable side always counts as a change."""

    def __eq__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return id(self)


MISSING = _Missing()


def infer_format(path_glob: str) -> str | None:
    """The format a glob's file extension implies, if it implies one."""
    suffix = Path(path_glob).suffix.lower()
    return {".json": "json", ".toml": "toml"}.get(suffix)


def validate_pointer(pointer: str) -> str:
    """A JSON Pointer: empty (the whole document) or '/'-separated tokens."""
    if pointer and not pointer.startswith("/"):
        raise RuleError(
            f"Field pointer {pointer!r} must be empty or start with '/' "
            "(a JSON Pointer such as /limits/max_depth)."
        )
    body = pointer.replace("~0", "").replace("~1", "")
    if "~" in body:
        raise RuleError(f"Field pointer {pointer!r} has an invalid '~' escape.")
    return pointer


def validate_format(fmt: str) -> str:
    if fmt not in FORMATS:
        raise RuleError(
            f"Unknown structured format {fmt!r}; expected one of: {', '.join(FORMATS)}."
        )
    return fmt


def _tokens(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def resolve(document: Any, pointer: str) -> Any:
    """The value a JSON Pointer names in a parsed document, or MISSING."""
    value = document
    for token in _tokens(pointer):
        if isinstance(value, dict):
            if token not in value:
                return MISSING
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or (token != "0" and token.startswith("0")):
                return MISSING
            index = int(token)
            if index >= len(value):
                return MISSING
            value = value[index]
        else:
            return MISSING
    return value


def parse(raw: bytes, fmt: str) -> Any:
    """Parse file contents, or return an unreadable marker."""
    try:
        text = raw.decode("utf-8")
        if fmt == "json":
            return json.loads(text)
        return tomllib.loads(text)
    except (UnicodeDecodeError, ValueError):
        return _Unreadable()


def canonical(value: Any) -> Any:
    """A comparable form of a resolved value.

    Python's == treats 1, 1.0 and True as equal. Canonical JSON text keeps
    those apart and ignores object key order. TOML dates render via str().
    """
    if value is MISSING or isinstance(value, _Unreadable):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _value(git_dir: str | Path, commit: str | None, path: str, rule: dict) -> Any:
    if commit is None:
        return MISSING
    raw = git_objects.blob_at(git_dir, commit, path)
    if raw is None:
        return MISSING
    document = parse(raw, rule["format"])
    if isinstance(document, _Unreadable):
        return document
    return canonical(resolve(document, rule["pointer"]))


def field_changes(
    git_dir: str | Path,
    oldrev: str,
    newrev: str,
    known_tips: list[str],
    rules: list[dict],
) -> list[dict]:
    """Protected values the update changes, as sorted {path, pointer} pairs.

    Only commits new to the repository are inspected (the same set as
    git_objects.changed_paths with the same known tips). A commit changes a
    value when it differs from the value in every parent, matching the
    combined-diff rule for paths: a clean merge changes nothing of its own.
    A file that cannot be parsed or read on either side counts as a change,
    so a rule fails closed rather than open.
    """
    if not rules or git_objects.is_zero(newrev):
        return []
    known = list(known_tips)
    if not git_objects.is_zero(oldrev):
        known.append(oldrev)
    changed: set[tuple[str, str]] = set()
    for commit in git_objects.new_commits(git_dir, newrev, known):
        paths = git_objects.commit_paths(git_dir, commit)
        parents: list[str | None] = list(git_objects.commit_parents(git_dir, commit))
        if not parents:
            parents = [None]
        for path in paths:
            for rule in rules:
                if (path, rule["pointer"]) in changed:
                    continue
                if not path_in_scopes(path, [rule["path_glob"]]):
                    continue
                after = _value(git_dir, commit, path, rule)
                before = [_value(git_dir, parent, path, rule) for parent in parents]
                if after is MISSING and all(value is MISSING for value in before):
                    # The commit lists this path, so the file exists on at
                    # least one side; reading none means the name could not
                    # be read back (not valid UTF-8). Fail closed.
                    changed.add((path, rule["pointer"]))
                elif all(after != value for value in before):
                    changed.add((path, rule["pointer"]))
    return [
        {"path": path, "pointer": pointer} for path, pointer in sorted(changed)
    ]
