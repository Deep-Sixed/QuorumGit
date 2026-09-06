"""Isolated git worktrees — one per active claim, never shared.

Git itself refuses to check out one branch in two worktrees, which is the
mechanical backstop for the one-writer-per-branch rule. Worktree operations
also participate in QuorumGit's ownership transaction before touching the
filesystem so a database rollback is never mistaken for a filesystem rollback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import audit
from .store import Connection, begin_immediate
from .work import get_claim, get_task, lock_task


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

    # Claim identity is part of the managed path. Re-claiming the same task by
    # the same agent therefore creates a fresh path without colliding with the
    # historical UNIQUE(worktrees.path) row from an earlier claim.
    wt_path = (
        worktrees_dir
        / task["repository"]
        / f"task-{task['id']}-{claim['agent']}-claim-{claim_id}"
    )
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        raise WorktreeError(f"Worktree path already exists: {wt_path}")

    branch_exists = (
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ]
        ).returncode
        == 0
    )
    if branch_exists:
        _git(repo_path, "worktree", "add", str(wt_path), branch)
    else:
        _git(repo_path, "worktree", "add", "-b", branch, str(wt_path), base_ref)

    try:
        row = conn.execute(
            """
            INSERT INTO worktrees (claim_id, path, branch)
            VALUES (?, ?, ?) RETURNING id
            """,
            (claim_id, str(wt_path), branch),
        ).fetchone()
        assert row is not None
        audit.record(
            conn,
            "worktree.created",
            "worktree",
            row[0],
            agent=claim["agent"],
            detail={"path": str(wt_path), "branch": branch},
        )
    except Exception:
        # Git succeeded but persistence failed. Best-effort compensation keeps
        # the common failure mode clean; doctor can reconcile if Git refuses.
        try:
            _git(repo_path, "worktree", "remove", str(wt_path))
        except WorktreeError:
            pass
        raise
    return {"id": row[0], "path": str(wt_path), "branch": branch}


def worktree_for_claim(conn: Connection, claim_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, path, branch, removed_at FROM worktrees WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "path": row[1], "branch": row[2], "removed_at": row[3]}


def transfer_worktree(conn: Connection, worktree_id: int, new_claim_id: int) -> None:
    """Reassign a worktree to a new claim (handoff continuation)."""
    conn.execute(
        "UPDATE worktrees SET claim_id = ? WHERE id = ?",
        (new_claim_id, worktree_id),
    )


def _mark_removed(
    conn: Connection,
    wt: dict,
    *,
    agent: str | None,
    event_type: str,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "UPDATE worktrees SET removed_at = unixepoch() WHERE id = ? AND removed_at IS NULL",
        (wt["id"],),
    )
    audit.record(
        conn,
        event_type,
        "worktree",
        wt["id"],
        agent=agent,
        detail={"path": wt["path"], **(detail or {})},
    )


def remove_worktree(conn: Connection, claim_id: int, agent: str) -> None:
    """Remove an active worktree only after reserving and proving ownership."""
    claim = get_claim(conn, claim_id)
    lock_task(conn, claim["task_id"])
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorktreeError(f"Claim {claim_id} is already released.")
    if claim["agent"] != agent:
        raise WorktreeError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    wt = worktree_for_claim(conn, claim_id)
    if wt is None or wt["removed_at"] is not None:
        raise WorktreeError(f"No active worktree for claim {claim_id}.")
    task = get_task(conn, claim["task_id"])
    _git(task["repository_path"], "worktree", "remove", wt["path"])
    _mark_removed(conn, wt, agent=agent, event_type="worktree.removed")


def cleanup_released_worktree(
    conn: Connection,
    claim_id: int,
    *,
    agent: str,
    reason: str,
) -> bool:
    """Remove a retained released-claim worktree without forcing dirty state.

    Handoff decline/cancel uses this after it has reserved the handoff/task.
    Git's ordinary `worktree remove` is deliberately used without --force; a
    dirty checkout therefore aborts the resolution rather than losing work.
    """
    wt = worktree_for_claim(conn, claim_id)
    if wt is None or wt["removed_at"] is not None:
        return False
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is None:
        raise WorktreeError(
            f"Claim {claim_id} is still active; refusing handoff cleanup."
        )
    task = get_task(conn, claim["task_id"])
    _git(task["repository_path"], "worktree", "remove", wt["path"])
    _mark_removed(
        conn,
        wt,
        agent=agent,
        event_type="worktree.handoff_cleanup",
        detail={"reason": reason},
    )
    return True


def doctor_worktrees(conn: Connection, repair: bool = False) -> list[dict]:
    """Report and conservatively repair drift in recorded managed worktrees.

    Doctor never scans for or adopts arbitrary directories. It only evaluates
    paths already recorded in `worktrees`. Repair uses ordinary non-forced Git
    removal, so dirty worktrees are never silently destroyed.
    """
    if repair:
        begin_immediate(conn)
    rows = conn.execute(
        """
        SELECT w.id, w.claim_id, w.path, w.branch, w.removed_at,
               c.released_at, t.id, r.path
        FROM worktrees w
        JOIN claims c ON c.id = w.claim_id
        JOIN tasks t ON t.id = c.task_id
        JOIN repositories r ON r.id = t.repository_id
        ORDER BY w.id
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        wt = {
            "id": row[0],
            "claim_id": row[1],
            "path": row[2],
            "branch": row[3],
            "removed_at": row[4],
        }
        released_at = row[5]
        repo_path = row[7]
        exists = Path(wt["path"]).exists()
        open_handoff = conn.execute(
            "SELECT 1 FROM handoffs WHERE from_claim_id = ? AND status = 'open' LIMIT 1",
            (wt["claim_id"],),
        ).fetchone() is not None

        issue: str | None = None
        if wt["removed_at"] is None and not exists:
            issue = "missing"
        elif wt["removed_at"] is None and released_at is not None and not open_handoff:
            issue = "orphaned"
        elif wt["removed_at"] is not None and exists:
            issue = "unexpectedly_present"
        if issue is None:
            continue

        finding = {
            "worktree_id": wt["id"],
            "claim_id": wt["claim_id"],
            "path": wt["path"],
            "issue": issue,
            "repaired": False,
        }
        if repair:
            try:
                if issue == "missing":
                    # The directory is already gone, but Git may still retain
                    # a stale worktree registration that would block reuse of
                    # the branch. Prune metadata before closing the DB record.
                    _git(repo_path, "worktree", "prune")
                    _mark_removed(
                        conn,
                        wt,
                        agent=None,
                        event_type="worktree.reconciled_missing",
                    )
                elif issue == "orphaned":
                    _git(repo_path, "worktree", "remove", wt["path"])
                    _mark_removed(
                        conn,
                        wt,
                        agent=None,
                        event_type="worktree.reconciled_orphan",
                    )
                else:  # recorded removed, but the recorded path is present again
                    _git(repo_path, "worktree", "remove", wt["path"])
                    audit.record(
                        conn,
                        "worktree.reconciled_unexpected",
                        "worktree",
                        wt["id"],
                        detail={"path": wt["path"]},
                    )
                finding["repaired"] = True
            except WorktreeError as exc:
                finding["error"] = str(exc)
        findings.append(finding)
    return findings


def head_commit(worktree_path: str | Path) -> str:
    return _git(worktree_path, "rev-parse", "HEAD")
