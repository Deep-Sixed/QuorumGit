"""Structured protection rules: values inside JSON and TOML files.

Derivation tests run against real Git repositories; hook tests are real
`git push`es through the installed hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from quorumgit import cli, gate, git_objects, registry, structured
from tests.conftest import OPERATOR, approve, ensure_agent
from tests.test_gate import _push, _setup

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}

STATE_RULE = {"path_glob": "config/state.json", "format": "json", "pointer": "/max_depth"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def _write(repo: Path, path: str, content: str, message: str = "edit") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _state(**values) -> str:
    return json.dumps(values, indent=2) + "\n"


@pytest.fixture()
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _write(path, "config/state.json", _state(max_depth=3, label="a"), "base")
    return path


def _changes(repo: Path, old: str, new: str, rules=(STATE_RULE,)) -> list[dict]:
    git_dir = git_objects.absolute_git_dir(repo)
    return structured.field_changes(git_dir, old, new, [], list(rules))


# ------------------------------------------------------------ rule semantics


def test_pointers_are_validated_and_resolved():
    assert structured.validate_pointer("") == ""
    assert structured.validate_pointer("/a~1b/~0c") == "/a~1b/~0c"
    for bad in ("max_depth", "/a~2b"):
        with pytest.raises(structured.RuleError):
            structured.validate_pointer(bad)
    document = {"a/b": {"~c": [10, 20]}}
    assert structured.resolve(document, "/a~1b/~0c/1") == 20
    assert structured.resolve(document, "/a~1b/~0c/2") is structured.MISSING
    assert structured.resolve(document, "/a~1b/~0c/01") is structured.MISSING
    assert structured.resolve(document, "") == document


def test_only_the_protected_value_counts(repo):
    base = _git(repo, "rev-parse", "HEAD")
    other = _write(repo, "config/state.json", _state(max_depth=3, label="b"))
    assert _changes(repo, base, other) == []

    changed = _write(repo, "config/state.json", _state(max_depth=4, label="b"))
    assert _changes(repo, other, changed) == [
        {"path": "config/state.json", "pointer": "/max_depth"}
    ]
    # Measured over every new commit, not the net result: a change that is
    # later reverted within the same push still counts.
    reverted = _write(repo, "config/state.json", _state(max_depth=3, label="b"))
    assert _changes(repo, other, reverted) == [
        {"path": "config/state.json", "pointer": "/max_depth"}
    ]


def test_comparison_is_by_value_and_type(repo):
    base = _git(repo, "rev-parse", "HEAD")
    reordered = _write(
        repo, "config/state.json", '{"label": "a", "max_depth": 3}\n'
    )
    assert _changes(repo, base, reordered) == []
    retyped = _write(repo, "config/state.json", _state(max_depth=3.0, label="a"))
    assert _changes(repo, reordered, retyped) != []
    boolean = _write(repo, "config/state.json", _state(max_depth=True, label="a"))
    assert _changes(repo, retyped, boolean) != []


def test_appearing_disappearing_and_unreadable_values_count(repo):
    base = _git(repo, "rev-parse", "HEAD")
    removed = _write(repo, "config/state.json", _state(label="a"))
    assert _changes(repo, base, removed) != []
    broken = _write(repo, "config/state.json", "{not json")
    assert _changes(repo, removed, broken) != []
    # Unreadable on both sides still fails closed.
    still_broken = _write(repo, "config/state.json", "{still not json")
    assert _changes(repo, broken, still_broken) != []
    _git(repo, "rm", "-q", "config/state.json")
    _git(repo, "commit", "-q", "-m", "drop")
    dropped = _git(repo, "rev-parse", "HEAD")
    other = _write(repo, "README.md", "unrelated\n")
    assert _changes(repo, dropped, other) == []


@pytest.mark.skipif(
    sys.platform != "linux", reason="needs a filesystem that accepts non-UTF-8 names"
)
def test_unreadable_file_names_fail_closed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    with open(os.fsencode(repo) + b"/config/caf\xe9.json", "wb") as handle:
        handle.write(b'{"max_depth": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "non-utf8 name")
    head = _git(repo, "rev-parse", "HEAD")
    rule = {**STATE_RULE, "path_glob": "config/*.json"}
    assert _changes(repo, base, head, [rule]) == [
        {"path": "config/caf\\xe9.json", "pointer": "/max_depth"}
    ]


def test_globs_and_toml(repo):
    base = _git(repo, "rev-parse", "HEAD")
    toml_rule = {"path_glob": "services/*/settings.toml", "format": "toml",
                 "pointer": "/limits/replicas"}
    edited = _write(
        repo, "services/api/settings.toml", "[limits]\nreplicas = 2\nmemory = 1\n"
    )
    assert _changes(repo, base, edited, [toml_rule]) == [
        {"path": "services/api/settings.toml", "pointer": "/limits/replicas"}
    ]
    memory_only = _write(
        repo, "services/api/settings.toml", "[limits]\nreplicas = 2\nmemory = 4\n"
    )
    assert _changes(repo, edited, memory_only, [toml_rule]) == []


def test_a_clean_merge_changes_nothing_of_its_own(repo):
    _git(repo, "checkout", "-q", "-b", "feat")
    feat = _write(repo, "src/app.py", "print(1)\n", "feature")
    _git(repo, "checkout", "-q", "main")
    main = _write(repo, "config/state.json", _state(max_depth=9, label="a"))
    _git(repo, "checkout", "-q", "feat")
    _git(repo, "merge", "-q", "--no-edit", "main")
    merged = _git(repo, "rev-parse", "HEAD")
    git_dir = git_objects.absolute_git_dir(repo)
    # main's change is already known to the repository; the merge adds none.
    assert structured.field_changes(
        git_dir, feat, merged, [main, feat], [STATE_RULE]
    ) == []


# ------------------------------------------------------------- registration


def test_rules_are_operator_only_and_validated(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "a-worker")
    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_protected_field(
            conn, approval_repo, "config/state.json", "/max_depth", actor="a-worker"
        )
    with pytest.raises(registry.RegistryError, match="--format"):
        registry.set_protected_field(
            conn, approval_repo, "config/state", "/max_depth", actor=OPERATOR
        )
    with pytest.raises(registry.RegistryError, match="JSON Pointer"):
        registry.set_protected_field(
            conn, approval_repo, "config/state.json", "max_depth", actor=OPERATOR
        )
    rules = registry.set_protected_field(
        conn, approval_repo, "config/state.json", "/max_depth", actor=OPERATOR
    )
    assert rules == [STATE_RULE]
    assert registry.set_protected_field(
        conn, approval_repo, "config/state", "/max_depth", actor=OPERATOR, fmt="toml"
    ) == [
        {"path_glob": "config/state", "format": "toml", "pointer": "/max_depth"},
        STATE_RULE,
    ]
    assert registry.set_protected_field(
        conn, approval_repo, "config/state", "/max_depth", actor=OPERATOR, remove=True
    ) == [STATE_RULE]
    assert "protected_field_update" in registry.OPERATION_TYPES


# ------------------------------------------------------------------- pushes


def _protected_hub(committed_conn, tmp_path):
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    ensure_agent(conn, OPERATOR, "operator")
    registry.set_protected_field(
        conn, repo_name, "config/state.json", "/max_depth", actor=OPERATOR
    )
    conn.commit()
    return repo_name, hub, clone, a, b


def test_hook_governs_only_the_protected_value(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _protected_hub(conn, tmp_path)
    _git(clone, "checkout", "-q", "-b", "feat/config")
    _write(clone, "config/state.json", _state(max_depth=3, label="a"))

    # A new file carrying the protected value is itself a change to it.
    first = _push(clone, a, "feat/config", cfg=cfg)
    assert first.returncode != 0
    assert "protected_field_update" in first.stderr
    assert "config/state.json#/max_depth" in first.stderr

    plan = gate.prepare_push(conn, repo_name, "feat/config", clone)
    assert plan["fields"] == [{"path": "config/state.json", "pointer": "/max_depth"}]
    entry = {e["operation"]["type"]: e for e in plan["operations"]}[
        "protected_field_update"
    ]
    assert entry["hash"] in first.stderr
    approve(conn, entry["operation"], requested_by=a)
    conn.commit()
    accepted = _push(clone, a, "feat/config", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
    assert gate.get_approval(conn, entry["hash"])["status"] == "consumed"

    # Other edits to the same file need nothing.
    _write(clone, "config/state.json", _state(max_depth=3, label="b"))
    other = _push(clone, a, "feat/config", cfg=cfg)
    assert other.returncode == 0, other.stderr

    # Changing the value again needs a fresh approval.
    _write(clone, "config/state.json", _state(max_depth=5, label="b"))
    again = _push(clone, a, "feat/config", cfg=cfg)
    assert again.returncode != 0
    assert "protected_field_update" in again.stderr


def test_per_operation_policy_applies_to_field_updates(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _protected_hub(conn, tmp_path)
    ensure_agent(conn, "op-2", "operator")
    registry.set_approval_policy(
        conn, repo_name, actor=OPERATOR,
        operation_type="protected_field_update", threshold=2,
    )
    conn.commit()
    _git(clone, "checkout", "-q", "-b", "feat/depth")
    _write(clone, "config/state.json", _state(max_depth=7))
    plan = gate.prepare_push(conn, repo_name, "feat/depth", clone)
    operation = plan["operations"][0]["operation"]
    approval = gate.request_approval(conn, operation, requested_by=a)
    assert approval["threshold"] == 2
    gate.vote(conn, approval["id"], OPERATOR, True)
    conn.commit()
    assert _push(clone, a, "feat/depth", cfg=cfg).returncode != 0
    gate.vote(conn, approval["id"], "op-2", True)
    conn.commit()
    pushed = _push(clone, a, "feat/depth", cfg=cfg)
    assert pushed.returncode == 0, pushed.stderr


def test_cli_manages_and_shows_rules(
    committed_conn, tmp_path, cfg, monkeypatch, capsys
):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    ensure_agent(conn, OPERATOR, "operator")
    conn.commit()
    monkeypatch.setenv("QUORUMGIT_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("QUORUMGIT_AGENT", OPERATOR)
    assert cli.main([
        "repo", "protect-field", repo_name, "config/*.json", "/max_depth",
    ]) == 0
    assert "config/*.json#/max_depth (json)" in capsys.readouterr().out
    assert cli.main(["repo", "policy", repo_name]) == 0
    assert "protected fields: config/*.json#/max_depth (json)" in (
        capsys.readouterr().out
    )

    _git(clone, "checkout", "-q", "-b", "feat/cli")
    _write(clone, "config/limits.json", _state(max_depth=2))
    monkeypatch.setenv("QUORUMGIT_AGENT", a)
    assert cli.main([
        "approve", "prepare", "--repo", repo_name, "--ref", "feat/cli",
        "-C", str(clone),
    ]) == 0
    out = capsys.readouterr().out
    assert "protected_field_update" in out
    assert "    config/limits.json#/max_depth" in out
