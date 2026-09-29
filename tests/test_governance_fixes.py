"""Regression tests for the second review round.

1. (Superseded by the repository approval policy tests on main.)
2. A successor claim on another branch still gets a fresh worktree; same-branch
   continuation is covered by main's worktree continuation tests.
3. and 4. (Scope normalization and push-time scope checks: now covered by
   main's tests/test_scope_enforcement.py and tests/test_content_governance.py.)
5. Commit specs resolve to full OIDs and user errors never print tracebacks.
6. `handoff create --last-commit` is honored when a worktree exists.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import handoff, registry, trees, work
from tests.conftest import make_git_repo
from tests.test_cli_hub import _cli

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def _local_setup(conn, tmp_path, agents=("a", "b", "op")):
    """A registered working repository (local model) plus named agents."""
    suffix = uuid.uuid4().hex[:8]
    repo_path = make_git_repo(tmp_path / f"repo-{suffix}")
    repo = f"local-{suffix}"
    registry.add_repository(conn, repo, repo_path)
    names = [f"{agent}-{suffix}" for agent in agents]
    for name in names:
        registry.add_agent(conn, name)
    task = work.create_task(conn, repo, "work")
    conn.commit()
    return repo, repo_path, task, names


def _claim_id(output: str) -> int:
    match = re.search(r"claim (\d+) acquired", output)
    assert match is not None, output
    return int(match.group(1))


def _worktree(conn, claim_id: int) -> dict:
    wt = trees.worktree_for_claim(conn, claim_id)
    assert wt is not None, f"claim {claim_id} has no worktree"
    return wt


def _no_traceback(result) -> None:
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.startswith("[quorumgit] ERROR:"), result.stderr


# ---------------------------------------------------------------- finding 2


def test_reclaim_on_a_different_branch_creates_a_fresh_worktree(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    _repo, _path, task, (a, b, _op) = _local_setup(conn, tmp_path)
    first = _cli(cfg, "claim", str(task), "--branch", "feat/old",
                 "--scope", "src/**", agent=a)
    assert first.returncode == 0, first.stderr
    old_claim = _claim_id(first.stdout)
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 10 WHERE id = ?",
        (old_claim,),
    )
    conn.commit()

    second = _cli(cfg, "claim", str(task), "--branch", "feat/new",
                  "--scope", "src/**", agent=b)
    assert second.returncode == 0, second.stderr
    assert "inherited" not in second.stdout
    new_claim = _claim_id(second.stdout)
    new_wt = trees.worktree_for_claim(conn, new_claim)
    assert new_wt is not None and new_wt["branch"] == "feat/new"
    assert _worktree(conn, old_claim)["removed_at"] is None


# ---------------------------------------------------------------- finding 5


def test_checkpoint_accepts_abbreviated_commit(committed_conn, tmp_path, cfg):
    conn = committed_conn
    _repo, repo_path, task, (a, _b, _op) = _local_setup(conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/short",
                   "--scope", "src/**", "--no-worktree", agent=a)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    full = _git(repo_path, "rev-parse", "main")

    for spec in (full[:9], full.upper(), "main"):
        result = _cli(cfg, "checkpoint", str(claim_id), "--commit", spec, agent=a)
        assert result.returncode == 0, result.stderr
        assert full in result.stdout

    bogus = _cli(cfg, "checkpoint", str(claim_id), "--commit", "nope", agent=a)
    _no_traceback(bogus)
    assert "does not exist" in bogus.stderr


def test_duplicate_registration_is_a_clean_error(committed_conn, tmp_path, cfg):
    conn = committed_conn
    _repo, _path, _task, (a, _b, _op) = _local_setup(conn, tmp_path)
    result = _cli(cfg, "agent", "add", a)
    _no_traceback(result)
    assert "already registered" in result.stderr


def test_duplicate_protected_ref_is_deduplicated(committed_conn, tmp_path):
    conn = committed_conn
    repo_path = make_git_repo(tmp_path / "dup-refs")
    name = f"dup-{uuid.uuid4().hex[:8]}"
    registry.add_repository(
        conn, name, repo_path,
        protected_refs=["refs/heads/main", "refs/heads/main"],
    )
    assert registry.get_repository(conn, name)["protected_refs"] == ["refs/heads/main"]


@pytest.mark.parametrize("payload", ["{bad", "[1, 2]"])
def test_invalid_operation_json_is_a_clean_error(cfg, initialized_store, payload):
    result = _cli(cfg, "approve", "hash", payload)
    _no_traceback(result)
    assert "Operation" in result.stderr


# ---------------------------------------------------------------- finding 6


def test_explicit_last_commit_wins_over_worktree_head(committed_conn, tmp_path, cfg):
    conn = committed_conn
    _repo, _path, task, (a, _b, _op) = _local_setup(conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/hand",
                   "--scope", "src/**", agent=a)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    wt_path = Path(_worktree(conn, claim_id)["path"])
    base = _git(wt_path, "rev-parse", "HEAD")
    (wt_path / "src" / "more.py").write_text("x\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-m", "later work")
    assert _git(wt_path, "rev-parse", "HEAD") != base

    created = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", "--last-commit", base[:10], agent=a)
    assert created.returncode == 0, created.stderr
    assert base in created.stdout
    match = re.search(r"handoff (\d+) created", created.stdout)
    assert match is not None, created.stdout
    hid = int(match.group(1))
    assert handoff.get_handoff(conn, hid)["record"]["last_commit"] == base


def test_handoff_after_worktree_removal_uses_last_commit(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    _repo, repo_path, task, (a, _b, _op) = _local_setup(conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/gone",
                   "--scope", "src/**", agent=a)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    trees.remove_worktree(conn, claim_id, a)
    conn.commit()

    missing = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", agent=a)
    assert missing.returncode == 1
    assert "--last-commit" in missing.stderr

    head = _git(repo_path, "rev-parse", "feat/gone")
    created = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", "--last-commit", head, agent=a)
    assert created.returncode == 0, created.stderr
