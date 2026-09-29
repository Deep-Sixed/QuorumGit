"""Isolated git worktrees — one per active claim, never shared.

Git itself refuses to check out one branch in two worktrees, which is the
mechanical backstop for the one-writer-per-branch rule. Worktree operations
also participate in QuorumGit's ownership transaction before touching the
filesystem so a database rollback is never mistaken for a filesystem rollback.
"""

from __future__ import annotations

import secrets
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


def _git_common_dir(repo_path: str | Path) -> Path:
    """Resolve Git's common directory without requiring newer rev-parse flags."""
    base = Path(repo_path).resolve()
    common = Path(_git(base, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = base / common
    return common.resolve()


def managed_worktree_path(
    worktrees_dir: Path, repository_id: int, claim_id: int
) -> Path:
    """Filesystem location for a new managed worktree.

    Built only from database-generated integer IDs and a random suffix:
    repository and agent names are display identities and never become path
    components, so no registered name can steer a checkout outside the
    directory QuorumGit owns. The suffix keeps the path fresh even when SQLite
    reuses the rowid of a rolled-back claim whose checkout was left behind.
    """
    root = Path(worktrees_dir).resolve()
    leaf = f"claim-{int(claim_id)}-{secrets.token_hex(4)}"
    path = (root / f"repo-{int(repository_id)}" / leaf).resolve()
    if not path.is_relative_to(root):
        raise WorktreeError(f"Managed worktree path escapes {root}: {path}")
    return path


def create_worktree(
    conn: Connection, claim_id: int, worktrees_dir: Path, base_ref: str = "HEAD"
) -> dict:
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorktreeError(f"Claim {claim_id} is released.")
    task = get_task(conn, claim["task_id"])
    repo_path = task["repository_path"]
    branch = claim["branch"]

    wt_path = managed_worktree_path(worktrees_dir, task["repository_id"], claim_id)
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


def _retained_worktree_for_branch(
    conn: Connection, repository_id: int, branch: str, exclude_claim_id: int
) -> dict | None:
    """A live recorded worktree with this branch checked out, if any."""
    row = conn.execute(
        """
        SELECT w.id, w.path, w.branch, c.id, c.task_id, c.released_at,
               c.release_reason,
               EXISTS (
                   SELECT 1 FROM handoffs h
                   WHERE h.from_claim_id = c.id AND h.status = 'open'
               )
        FROM worktrees w
        JOIN claims c ON c.id = w.claim_id
        JOIN tasks t ON t.id = c.task_id
        WHERE t.repository_id = ? AND w.branch = ? AND w.removed_at IS NULL
          AND c.id <> ?
        ORDER BY w.id DESC
        LIMIT 1
        """,
        (repository_id, branch, exclude_claim_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "path": row[1],
        "branch": row[2],
        "claim_id": row[3],
        "task_id": row[4],
        "released_at": row[5],
        "release_reason": row[6],
        "open_handoff": bool(row[7]),
    }


def continue_or_create_worktree(
    conn: Connection, claim_id: int, worktrees_dir: Path, base_ref: str = "HEAD"
) -> dict:
    """Give a new claim its worktree, continuing a retained one when it exists.

    A claim that supersedes an earlier claim on the same task and branch —
    after lease expiry, an approved takeover, or a release that kept its
    checkout — cannot get a second checkout: Git refuses to check one branch
    out twice. Instead the retained checkout, with any unfinished work in it,
    is verified and transferred to the new claim, and the transfer is audited.
    A retained checkout belonging to a different task is never adopted.
    """
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorktreeError(f"Claim {claim_id} is released.")
    task = get_task(conn, claim["task_id"])
    retained = _retained_worktree_for_branch(
        conn, task["repository_id"], claim["branch"], exclude_claim_id=claim_id
    )
    if retained is None or not Path(retained["path"]).exists():
        # A recorded checkout whose directory is gone holds no work to
        # continue; doctor reports and reconciles that record separately.
        return create_worktree(conn, claim_id, worktrees_dir, base_ref)

    where = (
        f"Branch {claim['branch']!r} is still checked out in retained worktree "
        f"{retained['path']} (claim {retained['claim_id']}, task "
        f"{retained['task_id']})"
    )
    if retained["task_id"] != task["id"]:
        raise WorktreeError(
            f"{where}. It belongs to another task and will not be adopted; "
            "its owner should release it with --remove-worktree, or claim with "
            "--no-worktree."
        )
    if retained["released_at"] is None or retained["open_handoff"]:
        raise WorktreeError(f"{where}, which is still reserved; refusing to adopt it.")
    issue, error = checkout_identity_issue(
        task["repository_path"], retained["path"], retained["branch"]
    )
    if issue is not None:
        raise WorktreeError(
            f"{where}, but its checkout no longer matches the record ({issue}"
            + (f": {error}" if error else "")
            + "). Inspect it manually; refusing to adopt it."
        )

    transfer_worktree(conn, retained["id"], claim_id)
    audit.record(
        conn,
        "worktree.continued",
        "worktree",
        retained["id"],
        agent=claim["agent"],
        detail={
            "path": retained["path"],
            "branch": retained["branch"],
            "from_claim_id": retained["claim_id"],
            "to_claim_id": claim_id,
            "from_release_reason": retained["release_reason"],
        },
    )
    return {
        "id": retained["id"],
        "path": retained["path"],
        "branch": retained["branch"],
        "continued_from_claim_id": retained["claim_id"],
    }


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


def checkout_identity_issue(
    repo_path: str | Path, wt_path: str | Path, branch: str
) -> tuple[str | None, str | None]:
    """Compare an existing checkout with its recorded repository and branch.

    Returns (issue, error): issue is None when the checkout is the recorded
    repository's worktree with the recorded branch checked out.
    """
    try:
        expected_common = _git_common_dir(repo_path)
        actual_common = _git_common_dir(wt_path)
        actual_root = Path(_git(wt_path, "rev-parse", "--show-toplevel")).resolve()
        actual_branch = _git(wt_path, "rev-parse", "--symbolic-full-name", "HEAD")
    except WorktreeError as exc:
        return "unverifiable_checkout", str(exc)
    if actual_common != expected_common or actual_root != Path(wt_path).resolve():
        return "repository_mismatch", None
    if actual_branch == "HEAD":
        return "detached_head", None
    if actual_branch != f"refs/heads/{branch}":
        return "branch_mismatch", None
    return None, None


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
        identity_error = None
        if exists:
            issue, identity_error = checkout_identity_issue(
                repo_path, wt["path"], wt["branch"]
            )
        if issue is not None:
            pass
        elif wt["removed_at"] is None and not exists:
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
        if issue in {
            "repository_mismatch",
            "branch_mismatch",
            "detached_head",
            "unverifiable_checkout",
        }:
            finding["error"] = identity_error or (
                "Checkout identity differs from its recorded ownership; inspect it manually."
            )
            findings.append(finding)
            continue
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
