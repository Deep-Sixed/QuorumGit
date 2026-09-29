"""A claim whose managed worktree was removed must not be treated as having one.

`doctor --repair` closes the record of a worktree whose directory vanished
while its claim stays live. Handoff and checkpoint must then fall back to an
explicit commit instead of running git in the deleted path, and a successor
must not be told to continue in it.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from quorumgit import handoff, registry, trees, work
from tests.test_cli_hub import _cli


def _claim_with_removed_worktree(conn, git_repo, worktrees_dir):
    suffix = uuid.uuid4().hex[:8]
    repo = f"repo-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    a, b = f"a-{suffix}", f"b-{suffix}"
    for name in (a, b):
        registry.add_agent(conn, name)
    task = work.create_task(conn, repo, "removed worktree")
    claim, _, _ = work.claim_task(
        conn, task, a, branch=f"feat/{suffix}", scope_globs=[f"src/{suffix}/**"]
    )
    wt = trees.create_worktree(conn, claim, worktrees_dir)

    # The state `doctor --repair` leaves for a vanished directory: the path is
    # gone, Git's registration pruned, and the record marked removed while the
    # claim itself is still live.
    shutil.rmtree(wt["path"])
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "prune"],
        check=True,
        capture_output=True,
    )
    record = trees.worktree_for_claim(conn, claim)
    assert record is not None
    trees._mark_removed(
        conn, record, agent=None, event_type="worktree.reconciled_missing"
    )
    assert trees.active_worktree_for_claim(conn, claim) is None
    return claim, a, b, wt["path"]


def _head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_accept_does_not_report_removed_worktree(conn, git_repo, cfg):
    claim, a, b, _ = _claim_with_removed_worktree(
        conn, git_repo, cfg.worktrees_dir
    )
    hid = handoff.create_handoff(
        conn,
        claim,
        a,
        {"completed": "part", "remaining": "rest", "last_commit": _head(git_repo)},
        to_agent=b,
    )

    result = handoff.accept_handoff(conn, hid, b)

    assert result["worktree"] is None
    # The removed record stays with the old claim rather than being transferred.
    assert trees.worktree_for_claim(conn, result["claim_id"]) is None


def test_cli_uses_explicit_commit_when_worktree_removed(
    committed_conn, git_repo, cfg
):
    conn = committed_conn
    claim, a, b, removed_path = _claim_with_removed_worktree(
        conn, git_repo, cfg.worktrees_dir
    )
    conn.commit()
    head = _head(git_repo)

    no_commit = _cli(cfg, "checkpoint", str(claim), agent=a)
    assert no_commit.returncode == 1
    assert "No active worktree" in no_commit.stderr

    checkpoint = _cli(cfg, "checkpoint", str(claim), "--commit", head, agent=a)
    assert checkpoint.returncode == 0, checkpoint.stderr

    missing = _cli(
        cfg, "handoff", "create", str(claim),
        "--completed", "part", "--remaining", "rest", "--to", b,
        agent=a,
    )
    assert missing.returncode == 1
    assert "--last-commit" in missing.stderr

    created = _cli(
        cfg, "handoff", "create", str(claim),
        "--completed", "part", "--remaining", "rest", "--to", b,
        "--last-commit", head,
        agent=a,
    )
    assert created.returncode == 0, created.stderr
    assert head in created.stdout
    hid = conn.execute(
        "SELECT id FROM handoffs WHERE from_claim_id = ?", (claim,)
    ).fetchone()[0]

    accepted = _cli(cfg, "handoff", "accept", str(hid), agent=b)
    assert accepted.returncode == 0, accepted.stderr
    assert "worktree: (none" in accepted.stdout
    assert removed_path not in accepted.stdout
