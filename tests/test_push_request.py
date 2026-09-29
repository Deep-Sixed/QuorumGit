"""`approve request --push` derives the exact operation the hook requires."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from quorumgit import gate, work
from tests.test_gate import _commit, _push, _register_agents, _setup

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


def _hub_has(hub, oid: str) -> bool:
    return subprocess.run(
        ["git", "--git-dir", str(hub), "cat-file", "-e", f"{oid}^{{commit}}"],
        capture_output=True, check=False,
    ).returncode == 0


def _approve(conn, operation) -> int:
    _register_agents(conn, "operator")
    approval = gate.request_approval(conn, operation, requested_by="operator")
    gate.vote(conn, approval["id"], "operator", True)
    conn.commit()
    return approval["id"]


def _cli(cfg, *args, agent=None):
    env = {**os.environ, "QUORUMGIT_DATA_DIR": str(cfg.data_dir)}
    env.pop("QUORUMGIT_AGENT", None)
    if agent:
        env["QUORUMGIT_AGENT"] = agent
    return subprocess.run(
        [sys.executable, "-m", "quorumgit", *args],
        capture_output=True, text=True, env=env, check=False,
    )


def test_derived_protected_push_matches_hook_and_lands(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "release.txt")
    newrev = _git(clone, "rev-parse", "HEAD")

    derived = gate.derive_push_operation(conn, repo_name, "main", a, source=clone)
    assert derived is not None
    assert derived == {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": _git(clone, "rev-parse", "origin/main"),
        "newrev": newrev,
    }
    assert not _hub_has(hub, newrev), "derivation must not write to the hub"

    rejected = _push(clone, a, "main", cfg=cfg)
    assert rejected.returncode != 0
    assert gate.operation_hash(derived) in rejected.stderr
    assert "approve request" in rejected.stderr

    _approve(conn, derived)
    assert _push(clone, a, "main", cfg=cfg).returncode == 0


def test_derived_out_of_scope_push_matches_hook(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "scoped")
    work.claim_task(conn, task, a, branch="feat/scoped", scope_globs=["src/**"])
    conn.commit()
    _commit(clone, "src/ok.py", branch="feat/scoped")
    _commit(clone, "docs/stray.md")

    derived = gate.derive_push_operation(
        conn, repo_name, "feat/scoped", a, source=clone
    )
    assert derived is not None
    assert derived["type"] == "out_of_scope_update"
    assert derived["out_of_scope_paths"] == ["docs/stray.md"]
    assert derived["oldrev"] == "0" * 40

    rejected = _push(clone, a, "feat/scoped", cfg=cfg)
    assert gate.operation_hash(derived) in rejected.stderr
    _approve(conn, derived)
    assert _push(clone, a, "feat/scoped", cfg=cfg).returncode == 0


def test_derived_force_push_and_delete(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "one.txt", branch="topic")
    assert _push(clone, a, "topic", cfg=cfg).returncode == 0
    published = _git(clone, "rev-parse", "HEAD")

    _git(clone, "reset", "--hard", "HEAD~1")
    _commit(clone, "rewritten.txt")
    forced = gate.derive_push_operation(conn, repo_name, "topic", a, source=clone)
    assert forced is not None
    assert forced["type"] == "force_update"
    assert forced["oldrev"] == published
    _approve(conn, forced)
    assert _push(clone, a, "+topic", cfg=cfg).returncode == 0

    deletion = gate.derive_push_operation(conn, repo_name, "topic", a, delete=True)
    assert deletion is not None
    assert deletion["type"] == "ref_delete"
    assert deletion["newrev"] == "0" * 40
    assert deletion["oldrev"] == _git(clone, "rev-parse", "HEAD")
    _approve(conn, deletion)
    assert _push(clone, a, ":topic", cfg=cfg).returncode == 0
    assert subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "--verify", "refs/heads/topic"],
        capture_output=True, check=False,
    ).returncode != 0


def test_ungoverned_push_derives_nothing(committed_conn, tmp_path):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "free.txt", branch="free")
    assert gate.derive_push_operation(conn, repo_name, "free", a, source=clone) is None


def test_derivation_enforces_branch_reservations(committed_conn, tmp_path):
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "owned")
    work.claim_task(conn, task, a, branch="feat/owned", scope_globs=["src/**"])
    conn.commit()
    _commit(clone, "src/x.py", branch="feat/owned")
    with pytest.raises(gate.PushRejected, match="claimed by"):
        gate.derive_push_operation(conn, repo_name, "feat/owned", b, source=clone)


def test_derivation_rejects_bad_inputs(committed_conn, tmp_path):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    with pytest.raises(gate.GateError, match="nothing to push"):
        gate.derive_push_operation(conn, repo_name, "main", a, source=clone)
    with pytest.raises(gate.GateError, match="Cannot resolve"):
        gate.derive_push_operation(
            conn, repo_name, "main", a, source=clone, rev="no-such-rev"
        )
    with pytest.raises(gate.GateError, match="nothing to delete"):
        gate.derive_push_operation(conn, repo_name, "absent", a, delete=True)


def test_cli_push_request_end_to_end(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _register_agents(conn, "operator")
    conn.commit()
    _commit(clone, "cli.txt")

    requested = _cli(
        cfg, "approve", "request", "--repo", repo_name, "--push", "main",
        "--from", str(clone), "--pusher", a, agent="operator",
    )
    assert requested.returncode == 0, requested.stderr
    assert '"type": "protected_ref_update"' in requested.stdout
    match = re.search(r"approval (\d+) hash=", requested.stdout)
    assert match
    voted = _cli(cfg, "approve", "vote", match.group(1), agent="operator")
    assert "status=approved" in voted.stdout

    assert _push(clone, a, "main", cfg=cfg).returncode == 0

    _commit(clone, "free.txt", branch="free")
    nothing = _cli(
        cfg, "approve", "request", "--repo", repo_name, "--push", "free",
        "--from", str(clone), agent=a,
    )
    assert nothing.returncode == 0
    assert "needs no approval" in nothing.stdout


def test_cli_push_request_usage_errors(cfg):
    both = _cli(cfg, "approve", "request", "{}", "--push", "main", agent="x")
    assert both.returncode == 2
    neither = _cli(cfg, "approve", "request", agent="x")
    assert neither.returncode == 2
    no_repo = _cli(cfg, "approve", "request", "--push", "main", "--from", ".",
                   agent="x")
    assert no_repo.returncode == 2
