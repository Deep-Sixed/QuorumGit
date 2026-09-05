"""Phases 1–2: registry, tasks, claims, leases, scopes, conflicts, worktrees."""

from __future__ import annotations

import uuid

import pytest

from quorumgit import registry, trees, work


def _setup(conn, git_repo, suffix=None):
    suffix = suffix or uuid.uuid4().hex[:8]
    repo_name = f"repo-{suffix}"
    registry.add_repository(conn, repo_name, git_repo)
    a, b = f"agent-a-{suffix}", f"agent-b-{suffix}"
    registry.add_agent(conn, a)
    registry.add_agent(conn, b)
    return repo_name, a, b


def test_repo_requires_git(conn, tmp_path):
    with pytest.raises(registry.RegistryError):
        registry.add_repository(conn, "not-git", tmp_path)


def test_claim_blocks_second_agent(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "do a thing")
    work.claim_task(conn, task, a, branch="feat/x", scope_globs=["src/**"])
    with pytest.raises(work.ClaimRefused, match="already claimed"):
        work.claim_task(conn, task, b, branch="feat/y", scope_globs=["docs/**"])


def test_scope_overlap_requires_acknowledgment(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    t1 = work.create_task(conn, repo, "task one")
    t2 = work.create_task(conn, repo, "task two")
    work.claim_task(conn, t1, a, branch="feat/x", scope_globs=["src/**"])
    with pytest.raises(work.ClaimRefused, match="overlap"):
        work.claim_task(conn, t2, b, branch="feat/y", scope_globs=["src/app*"])
    claim_id, classification, _ = work.claim_task(
        conn,
        t2,
        b,
        branch="feat/y",
        scope_globs=["src/app*"],
        override_overlap=True,
    )
    assert classification == "OVERLAPPING"
    assert claim_id


def test_disjoint_scopes_are_related(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    t1 = work.create_task(conn, repo, "task one")
    t2 = work.create_task(conn, repo, "task two")
    work.claim_task(conn, t1, a, branch="feat/x", scope_globs=["src/**"])
    _, classification, _ = work.claim_task(
        conn, t2, b, branch="feat/y", scope_globs=["docs/**"]
    )
    assert classification == "RELATED"


def test_branch_collision_is_conflicting(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    t1 = work.create_task(conn, repo, "task one")
    t2 = work.create_task(conn, repo, "task two")
    work.claim_task(conn, t1, a, branch="feat/x", scope_globs=["src/**"])
    with pytest.raises(work.ClaimRefused, match="Branch"):
        work.claim_task(conn, t2, b, branch="feat/x", scope_globs=["docs/**"])


def test_expired_lease_is_reclaimable(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "expiring")
    claim_id, _, _ = work.claim_task(
        conn,
        task,
        a,
        branch="feat/x",
        scope_globs=["src/**"],
        lease_hours=0.0001,
    )
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 60 WHERE id = ?",
        (claim_id,),
    )
    new_claim, classification, _ = work.claim_task(
        conn, task, b, branch="feat/z", scope_globs=["src/**"]
    )
    assert new_claim != claim_id
    old = work.get_claim(conn, claim_id)
    assert old["released_at"] is not None
    assert old["release_reason"] == "lease_expired"
    assert classification in ("CLEAR", "RELATED")


def test_renew_and_release_enforce_ownership(conn, git_repo):
    repo, a, b = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "ownership")
    claim_id, _, _ = work.claim_task(
        conn, task, a, branch="feat/x", scope_globs=["src/**"]
    )
    with pytest.raises(work.WorkError, match="belongs to"):
        work.renew_claim(conn, claim_id, b)
    with pytest.raises(work.WorkError, match="belongs to"):
        work.release_claim(conn, claim_id, b)
    work.release_claim(conn, claim_id, a)
    assert work.get_task(conn, task)["status"] == "open"


def test_worktree_isolation(conn, git_repo, cfg):
    repo, a, b = _setup(conn, git_repo)
    t1 = work.create_task(conn, repo, "task one")
    t2 = work.create_task(conn, repo, "task two")
    c1, _, _ = work.claim_task(
        conn, t1, a, branch="feat/one", scope_globs=["src/**"]
    )
    c2, _, _ = work.claim_task(
        conn, t2, b, branch="feat/two", scope_globs=["docs/**"]
    )
    w1 = trees.create_worktree(conn, c1, cfg.worktrees_dir)
    w2 = trees.create_worktree(conn, c2, cfg.worktrees_dir)
    assert w1["path"] != w2["path"]
    assert trees.head_commit(w1["path"]) == trees.head_commit(w2["path"])
    cp = work.add_checkpoint(
        conn, c1, a, trees.head_commit(w1["path"]), note="start"
    )
    assert cp
