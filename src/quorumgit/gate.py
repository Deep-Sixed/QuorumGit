"""Approval gate and pre-receive enforcement.

Protected operations require an approval whose hash binds to the exact
operation payload. Enforcement is fail-closed: any hook error rejects the push.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from . import audit
from .canonical import stable_hash
from .registry import get_repository
from .store import Connection, begin_immediate, json_dumps, json_loads
from .work import live_claim_for_branch, open_handoff_for_branch

DEFAULT_THRESHOLD = 1


class GateError(RuntimeError):
    pass


class PushRejected(GateError):
    pass


# ---------------------------------------------------------------- approvals


def operation_hash(operation: dict[str, Any]) -> str:
    if not operation.get("type") or not operation.get("repository"):
        raise GateError("Operation requires 'type' and 'repository' fields.")
    return stable_hash(operation)


def request_approval(
    conn: Connection,
    operation: dict[str, Any],
    requested_by: str,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict:
    """Create (or return the existing) approval request for an operation."""
    op_hash = operation_hash(operation)
    row = conn.execute(
        """
        INSERT INTO approvals (operation_hash, operation, threshold)
        VALUES (?, ?, ?)
        ON CONFLICT (operation_hash) DO NOTHING
        RETURNING id
        """,
        (op_hash, json_dumps(operation), threshold),
    ).fetchone()
    if row:
        audit.record(
            conn,
            "approval.requested",
            "approval",
            row[0],
            agent=requested_by,
            detail={"operation": operation, "hash": op_hash},
        )
    return get_approval(conn, op_hash)


def get_approval(conn: Connection, op_hash: str) -> dict:
    row = conn.execute(
        """
        SELECT id, operation_hash, operation, threshold, status
        FROM approvals WHERE operation_hash = ?
        """,
        (op_hash,),
    ).fetchone()
    if row is None:
        raise GateError(f"No approval request exists for {op_hash}")
    return {
        "id": row[0],
        "operation_hash": row[1],
        "operation": json_loads(row[2], {}),
        "threshold": row[3],
        "status": row[4],
    }


def vote(conn: Connection, op_hash: str, voter: str, approve: bool) -> dict:
    """Record a vote and decide the approval atomically.

    BEGIN IMMEDIATE serializes competing voters before either reads the current
    approval state. Denial has precedence and terminal states remain final.
    """
    begin_immediate(conn)
    approval = get_approval(conn, op_hash)
    if approval["status"] != "pending":
        raise GateError(f"Approval {op_hash} is already {approval['status']}.")
    threshold = approval["threshold"]
    conn.execute(
        """
        INSERT INTO votes (approval_id, voter, vote) VALUES (?, ?, ?)
        ON CONFLICT (approval_id, voter) DO UPDATE SET vote = excluded.vote
        """,
        (approval["id"], voter, approve),
    )
    audit.record(
        conn,
        "approval.vote",
        "approval",
        approval["id"],
        agent=voter,
        detail={"vote": approve, "hash": op_hash},
    )

    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE vote = 1), "
        "count(*) FILTER (WHERE vote = 0) "
        "FROM votes WHERE approval_id = ?",
        (approval["id"],),
    ).fetchone()
    assert counts is not None
    yes, no = counts
    if no > 0:
        new_status = "denied"
    elif yes >= threshold:
        new_status = "approved"
    else:
        new_status = "pending"
    if new_status != "pending":
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = unixepoch() "
            "WHERE id = ? AND status = 'pending'",
            (new_status, approval["id"]),
        )
        audit.record(
            conn,
            f"approval.{new_status}",
            "approval",
            approval["id"],
            detail={"hash": op_hash},
        )
    return get_approval(conn, op_hash)


def is_approved(conn: Connection, operation: dict[str, Any]) -> bool:
    """True only if an approval bound to this exact operation is approved."""
    try:
        approval = get_approval(conn, operation_hash(operation))
    except GateError:
        return False
    return approval["status"] == "approved" and approval["operation"] == operation


def consume_approval(conn: Connection, operation: dict[str, Any], agent: str) -> None:
    """Atomically consume one approved exact-operation authorization."""
    begin_immediate(conn)
    op_hash = operation_hash(operation)
    approval = get_approval(conn, op_hash)
    if approval["status"] != "approved":
        raise GateError(
            f"Approval {op_hash} is not consumable (status "
            f"{approval['status']}); it may already be used."
        )
    cur = conn.execute(
        "UPDATE approvals SET status = 'denied', decided_at = unixepoch() "
        "WHERE id = ? AND status = 'approved'",
        (approval["id"],),
    )
    if cur.rowcount != 1:
        raise GateError(
            f"Approval {op_hash} was consumed concurrently; refusing to "
            "authorize twice."
        )
    audit.record(
        conn,
        "approval.consumed",
        "approval",
        approval["id"],
        agent=agent,
        detail={"hash": op_hash},
    )


# --------------------------------------------------------------- hook logic


ZERO_OID = "0" * 40


def _is_zero(oid: str) -> bool:
    return set(oid) == {"0"}


def _is_fast_forward(git_dir: str, oldrev: str, newrev: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "--git-dir",
            git_dir,
            "merge-base",
            "--is-ancestor",
            oldrev,
            newrev,
        ],
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise PushRejected("Unable to determine fast-forward status.")
    return result.returncode == 0


def check_ref_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str | None,
    oldrev: str,
    newrev: str,
    refname: str,
) -> None:
    """Enforce governance for one ref update. Raises PushRejected."""
    repo = get_repository(conn, repository)
    branch = refname.removeprefix("refs/heads/")

    if refname.startswith("refs/heads/"):
        pending = open_handoff_for_branch(conn, repo["id"], branch)
        if pending:
            raise PushRejected(
                f"Branch {branch!r} is frozen pending handoff "
                f"{pending['id']}; accept the handoff before pushing."
            )
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim and claim["agent"] != pusher:
            raise PushRejected(
                f"Branch {branch!r} is claimed by {claim['agent']} "
                f"(claim {claim['id']}); pusher is "
                f"{pusher or 'unidentified — set QUORUMGIT_AGENT'}."
            )

    protected = refname in repo["protected_refs"]
    deletion = _is_zero(newrev)
    forced = (
        not deletion
        and not _is_zero(oldrev)
        and not _is_fast_forward(git_dir, oldrev, newrev)
    )

    if protected or deletion or forced:
        operation = {
            "type": "protected_ref_update"
            if protected
            else ("ref_delete" if deletion else "force_update"),
            "repository": repository,
            "refname": refname,
            "oldrev": oldrev,
            "newrev": newrev,
        }
        if not is_approved(conn, operation):
            raise PushRejected(
                f"{operation['type']} on {refname} requires an approval "
                f"bound to this exact update (hash {operation_hash(operation)})."
            )
        consume_approval(conn, operation, agent=pusher or "")
        audit.record(
            conn,
            "gate.protected_update_allowed",
            "repository",
            repo["id"],
            agent=pusher,
            detail=operation,
        )
        return

    audit.record(
        conn,
        "gate.update_allowed",
        "repository",
        repo["id"],
        agent=pusher,
        detail={"refname": refname, "oldrev": oldrev, "newrev": newrev},
    )


def run_pre_receive(
    conn: Connection, repository: str, stdin_lines: Iterable[str]
) -> int:
    """Hook entry point. Reads `oldrev newrev refname` lines. Fail-closed."""
    git_dir = os.environ.get("GIT_DIR")
    if not git_dir:
        print("[quorumgit] REJECTED: GIT_DIR is not set.", file=sys.stderr)
        return 1
    pusher = os.environ.get("QUORUMGIT_AGENT") or None
    try:
        begin_immediate(conn)
        saw_update = False
        for line in stdin_lines:
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 3:
                raise PushRejected(f"Malformed pre-receive input: {line!r}")
            saw_update = True
            check_ref_update(conn, repository, git_dir, pusher, *parts)
        if not saw_update:
            raise PushRejected("No ref updates supplied on stdin.")
    except Exception as exc:  # fail closed on anything
        conn.rollback()
        print(f"[quorumgit] REJECTED: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print("[quorumgit] accepted.")
    return 0


def install_hook(conn: Connection, repository: str) -> Path:
    """Install the pre-receive hook into the repository's git directory."""
    repo = get_repository(conn, repository)
    git_dir = Path(
        subprocess.run(
            ["git", "-C", repo["path"], "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -m quorumgit hook pre-receive '
        f'--repo "{repository}"\n',
        encoding="utf-8",
    )
    hook_path.chmod(0o755)
    audit.record(
        conn,
        "hook.installed",
        "repository",
        repo["id"],
        detail={"path": str(hook_path)},
    )
    return hook_path
