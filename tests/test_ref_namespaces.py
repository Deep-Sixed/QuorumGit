"""Refs outside refs/heads/ need approval unless their namespace is open."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from quorumgit import audit, gate, registry
from tests.test_gate import _push, _register_agents, _setup

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=GIT_ENV,
    ).stdout.strip()


def _approve(conn, operation) -> None:
    _register_agents(conn, "operator")
    approval = gate.request_approval(conn, operation, requested_by="operator")
    gate.vote(conn, approval["id"], "operator", True)
    conn.commit()


def test_tag_push_requires_exact_approval(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _git(clone, "tag", "v1.0")

    rejected = _push(clone, a, "refs/tags/v1.0", cfg=cfg)
    assert rejected.returncode != 0
    assert "non_branch_ref_update" in rejected.stderr

    operation = gate.derive_push_operation(
        conn, repo_name, "refs/tags/v1.0", a, source=clone, rev="v1.0"
    )
    assert operation is not None
    assert operation == {
        "type": "non_branch_ref_update",
        "repository": repo_name,
        "refname": "refs/tags/v1.0",
        "oldrev": "0" * 40,
        "newrev": _git(clone, "rev-parse", "HEAD"),
    }
    assert gate.operation_hash(operation) in rejected.stderr
    _approve(conn, operation)
    assert _push(clone, a, "refs/tags/v1.0", cfg=cfg).returncode == 0


def test_annotated_tag_derivation_uses_the_tag_object(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _git(clone, "tag", "-a", "v2.0", "-m", "release 2.0")
    tag_object = _git(clone, "rev-parse", "v2.0")
    assert tag_object != _git(clone, "rev-parse", "v2.0^{commit}")

    operation = gate.derive_push_operation(
        conn, repo_name, "refs/tags/v2.0", a, source=clone, rev="v2.0"
    )
    assert operation is not None
    assert operation["newrev"] == tag_object
    rejected = _push(clone, a, "refs/tags/v2.0", cfg=cfg)
    assert gate.operation_hash(operation) in rejected.stderr
    _approve(conn, operation)
    assert _push(clone, a, "refs/tags/v2.0", cfg=cfg).returncode == 0


def test_custom_namespace_is_governed(committed_conn, tmp_path, cfg):
    _repo_name, _hub, clone, a, _b = _setup(committed_conn, tmp_path)
    result = _push(clone, a, "HEAD:refs/custom/marker", cfg=cfg)
    assert result.returncode != 0
    assert "non_branch_ref_update" in result.stderr


def test_open_namespace_skips_approval_until_closed(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    registry.open_namespace(conn, repo_name, "refs/tags")
    conn.commit()

    _git(clone, "tag", "open-1")
    assert _push(clone, a, "refs/tags/open-1", cfg=cfg).returncode == 0
    # Other namespaces stay governed.
    assert _push(clone, a, "HEAD:refs/custom/x", cfg=cfg).returncode != 0

    registry.close_namespace(conn, repo_name, "refs/tags/")
    conn.commit()
    _git(clone, "tag", "closed-1")
    assert _push(clone, a, "refs/tags/closed-1", cfg=cfg).returncode != 0


def test_open_namespace_still_governs_deletes_and_protected_refs(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    registry.open_namespace(conn, repo_name, "refs/tags/")
    conn.execute(
        "INSERT INTO protected_refs (repository_id, refname) VALUES (?, ?)",
        (registry.get_repository(conn, repo_name)["id"], "refs/tags/stable"),
    )
    conn.commit()

    _git(clone, "tag", "temp")
    assert _push(clone, a, "refs/tags/temp", cfg=cfg).returncode == 0
    delete = _push(clone, a, ":refs/tags/temp", cfg=cfg)
    assert delete.returncode != 0
    assert "ref_delete" in delete.stderr

    _git(clone, "tag", "stable")
    protected = _push(clone, a, "refs/tags/stable", cfg=cfg)
    assert protected.returncode != 0
    assert "protected_ref_update" in protected.stderr


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        ("refs/heads/", "cannot be opened"),
        ("refs/heads/release/", "cannot be opened"),
        ("tags/", "must look like"),
        ("refs/", "must look like"),
    ],
)
def test_namespace_validation(conn, git_repo, prefix, message):
    name = "ns-validate"
    registry.add_repository(conn, name, git_repo)
    with pytest.raises(registry.RegistryError, match=message):
        registry.open_namespace(conn, name, prefix)


def test_namespace_lifecycle_is_audited(conn, git_repo):
    registry.add_repository(conn, "ns-audit", git_repo)
    assert registry.open_namespace(conn, "ns-audit", "refs/notes") == "refs/notes/"
    with pytest.raises(registry.RegistryError, match="already open"):
        registry.open_namespace(conn, "ns-audit", "refs/notes/")
    registry.close_namespace(conn, "ns-audit", "refs/notes/")
    with pytest.raises(registry.RegistryError, match="not open"):
        registry.close_namespace(conn, "ns-audit", "refs/notes/")
    repo_id = registry.get_repository(conn, "ns-audit")["id"]
    kinds = [
        e["event_type"]
        for e in audit.events(conn, entity="repository", entity_id=repo_id)
    ]
    assert "repository.namespace_opened" in kinds
    assert "repository.namespace_closed" in kinds


def test_cli_namespace_commands(committed_conn, git_repo, cfg):
    registry.add_repository(committed_conn, "ns-cli", git_repo)
    committed_conn.commit()

    def cli(*args):
        env = {**os.environ, "QUORUMGIT_DATA_DIR": str(cfg.data_dir)}
        return subprocess.run(
            [sys.executable, "-m", "quorumgit", *args],
            capture_output=True, text=True, env=env, check=False,
        )

    opened = cli("repo", "namespace", "open", "ns-cli", "refs/tags/")
    assert opened.returncode == 0, opened.stderr
    assert cli("repo", "namespace", "list", "ns-cli").stdout.split() == ["refs/tags/"]
    closed = cli("repo", "namespace", "close", "ns-cli", "refs/tags/")
    assert closed.returncode == 0, closed.stderr
    assert cli("repo", "namespace", "list", "ns-cli").stdout.strip() == ""
    refused = cli("repo", "namespace", "open", "ns-cli", "refs/heads/")
    assert refused.returncode == 1
