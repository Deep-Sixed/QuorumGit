"""Isolated git worktrees — one per active claim, never shared.

Git itself refuses to check out one branch in two worktrees, which is the
mechanical backstop for the one-writer-per-branch rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from psycopg import Connection

from . import audit
from .work import get_claim, get_task


class WorktreeError(RuntimeError):
    pass


def _git(repo_path: str | Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def create_worktree(
    conn: Connection, claim_id: int, worktrees_dir: Path, base_ref: str = "HEAD"
) -> dict:
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorktreeError(f"Claim {claim_id} is released.")
    task = get_task(conn, claim["task_id"])
    repo_path = task["repository_path"]
    branch = claim["branch"]

    wt_path = worktrees_dir / task["repository"] / f"task-{task['id']}-{claim['agent']}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        raise WorktreeError(f"Worktree path already exists: {wt_path}")

    branch_exists = subprocess.run(
        ["git", "-C", repo_path, "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch}"],
    ).returncode == 0
    if branch_exists:
        _git(repo_path, "worktree", "add", str(wt_path), branch)
    else:
        _git(repo_path, "worktree", "add", "-b", branch, str(wt_path), base_ref)

    row = conn.execute(
        """
        INSERT INTO worktrees (claim_id, path, branch)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (claim_id, str(wt_path), branch),
    ).fetchone()
    assert row is not None
    audit.record(conn, "worktree.created", "worktree", row[0],
                 agent=claim["agent"],
                 detail={"path": str(wt_path), "branch": branch})
    return {"id": row[0], "path": str(wt_path), "branch": branch}


def worktree_for_claim(conn: Connection, claim_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, path, branch, removed_at FROM worktrees WHERE claim_id = %s
        """,
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "path": row[1], "branch": row[2], "removed_at": row[3]}


def transfer_worktree(conn: Connection, worktree_id: int, new_claim_id: int) -> None:
    """Reassign a worktree to a new claim (handoff continuation)."""
    conn.execute(
        "UPDATE worktrees SET claim_id = %s WHERE id = %s",
        (new_claim_id, worktree_id),
    )


def remove_worktree(conn: Connection, claim_id: int, agent: str) -> None:
    wt = worktree_for_claim(conn, claim_id)
    if wt is None or wt["removed_at"] is not None:
        raise WorktreeError(f"No active worktree for claim {claim_id}.")
    task = get_task(conn, get_claim(conn, claim_id)["task_id"])
    _git(task["repository_path"], "worktree", "remove", wt["path"])
    conn.execute(
        "UPDATE worktrees SET removed_at = now() WHERE id = %s", (wt["id"],)
    )
    audit.record(conn, "worktree.removed", "worktree", wt["id"], agent=agent,
                 detail={"path": wt["path"]})


def head_commit(worktree_path: str | Path) -> str:
    return _git(worktree_path, "rev-parse", "HEAD")
