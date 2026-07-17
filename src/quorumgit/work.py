"""Tasks, claims, leases, declared scopes, and conflict classification.

Leases are timestamps, not timers: expiry is evaluated whenever a claim is
read, and an expired claim is explicitly released (with an audit event) at
the moment another operation supersedes it. No background process exists.
"""

from __future__ import annotations

import subprocess
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from . import audit
from .registry import get_agent, get_repository

DEFAULT_LEASE_HOURS = 8

CLASSIFICATIONS = ("CLEAR", "RELATED", "OVERLAPPING", "CONFLICTING", "BLOCKED")


class WorkError(RuntimeError):
    pass


class ClaimRefused(WorkError):
    """The claim was refused by governance rules; message says why."""


# ------------------------------------------------------------------- tasks


def create_task(
    conn: Connection, repository: str, title: str, objective: str = ""
) -> int:
    repo = get_repository(conn, repository)
    row = conn.execute(
        """
        INSERT INTO tasks (repository_id, title, objective)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (repo["id"], title, objective),
    ).fetchone()
    assert row is not None
    task_id = row[0]
    audit.record(conn, "task.created", "task", task_id,
                 detail={"repository": repository, "title": title})
    return task_id


def get_task(conn: Connection, task_id: int) -> dict:
    row = conn.execute(
        """
        SELECT t.id, t.repository_id, r.name, r.path, t.title, t.objective,
               t.status
        FROM tasks t JOIN repositories r ON r.id = t.repository_id
        WHERE t.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise WorkError(f"No such task: {task_id}")
    return {
        "id": row[0],
        "repository_id": row[1],
        "repository": row[2],
        "repository_path": row[3],
        "title": row[4],
        "objective": row[5],
        "status": row[6],
    }


def list_tasks(conn: Connection, repository: str | None = None) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.id, r.name, t.title, t.status
        FROM tasks t JOIN repositories r ON r.id = t.repository_id
        WHERE (%s::text IS NULL OR r.name = %s)
        ORDER BY t.id
        """,
        (repository, repository),
    ).fetchall()
    return [
        {"id": r[0], "repository": r[1], "title": r[2], "status": r[3]}
        for r in rows
    ]


def set_task_status(conn: Connection, task_id: int, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = %s, updated_at = now() WHERE id = %s",
        (status, task_id),
    )


# ------------------------------------------------------------------- claims


def lock_task(conn: Connection, task_id: int) -> None:
    """Serialize claim ownership transitions for one task.

    Takeover approval binds to the incumbent observed while this lock is held;
    normal claims and releases take the same lock, so that incumbent cannot be
    replaced between approval validation and the atomic takeover.
    """
    row = conn.execute(
        "SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
    ).fetchone()
    if row is None:
        raise WorkError(f"No such task: {task_id}")


def _claim_row(row) -> dict:
    keys = (
        "id", "task_id", "agent_id", "agent", "branch",
        "lease_expires_at", "released_at", "release_reason", "expired",
    )
    return dict(zip(keys, row))


def get_claim(conn: Connection, claim_id: int) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.task_id, c.agent_id, a.name, c.branch,
               c.lease_expires_at, c.released_at, c.release_reason,
               (c.lease_expires_at < now()) AS expired
        FROM claims c JOIN agents a ON a.id = c.agent_id
        WHERE c.id = %s
        """,
        (claim_id,),
    ).fetchone()
    if row is None:
        raise WorkError(f"No such claim: {claim_id}")
    return _claim_row(row)


def active_claim_for_task(conn: Connection, task_id: int) -> dict | None:
    """The unreleased claim on a task, expired or not. None if released."""
    row = conn.execute(
        """
        SELECT c.id, c.task_id, c.agent_id, a.name, c.branch,
               c.lease_expires_at, c.released_at, c.release_reason,
               (c.lease_expires_at < now()) AS expired
        FROM claims c JOIN agents a ON a.id = c.agent_id
        WHERE c.task_id = %s AND c.released_at IS NULL
        """,
        (task_id,),
    ).fetchone()
    return _claim_row(row) if row else None


def live_claims_in_repository(
    conn: Connection, repository_id: int, exclude_task: int | None = None
) -> list[dict]:
    """Unreleased, unexpired claims in a repository, with their scopes."""
    rows = conn.execute(
        """
        SELECT c.id, c.task_id, a.name, c.branch,
               COALESCE(array_agg(s.path_glob) FILTER (WHERE s.id IS NOT NULL), '{}')
        FROM claims c
        JOIN tasks t ON t.id = c.task_id
        JOIN agents a ON a.id = c.agent_id
        LEFT JOIN scopes s ON s.claim_id = c.id
        WHERE t.repository_id = %s
          AND c.released_at IS NULL
          AND c.lease_expires_at >= now()
          AND (%s::bigint IS NULL OR c.task_id <> %s)
        GROUP BY c.id, c.task_id, a.name, c.branch
        """,
        (repository_id, exclude_task, exclude_task),
    ).fetchall()
    return [
        {"id": r[0], "task_id": r[1], "agent": r[2], "branch": r[3], "scopes": r[4]}
        for r in rows
    ]


def open_handoff_for_task(conn: Connection, task_id: int) -> dict | None:
    """An open (unaccepted, undeclined) handoff reserving this task, if any."""
    row = conn.execute(
        """
        SELECT h.id, a.name
        FROM handoffs h
        LEFT JOIN agents a ON a.id = h.to_agent_id
        WHERE h.task_id = %s AND h.status = 'open'
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return {"id": row[0], "to_agent": row[1]} if row else None


def open_handoff_for_branch(
    conn: Connection, repository_id: int, branch: str
) -> dict | None:
    """An open handoff that reserves this branch during the handoff gap."""
    row = conn.execute(
        """
        SELECT h.id
        FROM handoffs h
        JOIN claims c ON c.id = h.from_claim_id
        JOIN tasks t ON t.id = h.task_id
        WHERE t.repository_id = %s AND c.branch = %s AND h.status = 'open'
        LIMIT 1
        """,
        (repository_id, branch),
    ).fetchone()
    return {"id": row[0]} if row else None


def live_claim_for_branch(
    conn: Connection, repository_id: int, branch: str
) -> dict | None:
    row = conn.execute(
        """
        SELECT c.id, c.task_id, c.agent_id, a.name, c.branch,
               c.lease_expires_at, c.released_at, c.release_reason,
               (c.lease_expires_at < now()) AS expired
        FROM claims c
        JOIN tasks t ON t.id = c.task_id
        JOIN agents a ON a.id = c.agent_id
        WHERE t.repository_id = %s AND c.branch = %s
          AND c.released_at IS NULL AND c.lease_expires_at >= now()
        LIMIT 1
        """,
        (repository_id, branch),
    ).fetchone()
    return _claim_row(row) if row else None


# --------------------------------------------------------- scope comparison


def _glob_prefix(glob: str) -> str:
    """Literal path prefix of a glob (up to the first wildcard character)."""
    for i, ch in enumerate(glob):
        if ch in "*?[":
            return glob[:i]
    return glob


def scopes_overlap(a: str, b: str) -> bool:
    """Conservative overlap test: two scopes overlap when the literal prefix
    of one is a prefix of the other. Predictable and errs toward flagging."""
    pa, pb = _glob_prefix(a), _glob_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def verify_commit(
    repo_path: str, commit_oid: str, branch: str | None = None
) -> None:
    """The OID must exist as a commit in the repository. When the branch ref
    exists, the commit must also be reachable from it — a continuation point
    nobody can reach is not a continuation point."""
    exists = subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", f"{commit_oid}^{{commit}}"],
        capture_output=True,
    ).returncode == 0
    if not exists:
        raise WorkError(
            f"Commit {commit_oid} does not exist in the registered repository."
        )
    if branch:
        branch_exists = subprocess.run(
            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode == 0
        if branch_exists:
            reachable = subprocess.run(
                ["git", "-C", str(repo_path), "merge-base", "--is-ancestor",
                 commit_oid, f"refs/heads/{branch}"],
                capture_output=True,
            ).returncode == 0
            if not reachable:
                raise WorkError(
                    f"Commit {commit_oid} is not reachable from branch "
                    f"{branch!r} in the registered repository."
                )


def classify(
    conn: Connection,
    repository_id: int,
    task_id: int,
    branch: str,
    scope_globs: list[str],
    exclude_claim_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Classify a prospective claim against all live claims in the repo.

    exclude_claim_id skips one claim in the BLOCKED check — the holder being
    displaced by an approved takeover must not block its own replacement.
    """
    active = active_claim_for_task(conn, task_id)
    if active and not active["expired"] and active["id"] != exclude_claim_id:
        return "BLOCKED", {
            "reason": "task already claimed",
            "holder": active["agent"],
            "claim_id": active["id"],
        }

    others = live_claims_in_repository(conn, repository_id, exclude_task=task_id)
    if not others:
        return "CLEAR", {}

    conflicting = [o for o in others if o["branch"] == branch]
    if conflicting:
        return "CONFLICTING", {
            "reason": "branch already claimed",
            "branch": branch,
            "holders": [o["agent"] for o in conflicting],
        }

    overlaps = []
    for other in others:
        hits = [
            {"theirs": og, "ours": sg}
            for og in other["scopes"]
            for sg in scope_globs
            if scopes_overlap(og, sg)
        ]
        if hits:
            overlaps.append({"claim_id": other["id"], "agent": other["agent"],
                             "matches": hits})
    if overlaps:
        return "OVERLAPPING", {"overlaps": overlaps}

    return "RELATED", {"live_claims": len(others)}


# ------------------------------------------------------------ claim actions


def claim_task(
    conn: Connection,
    task_id: int,
    agent: str,
    branch: str,
    scope_globs: list[str],
    lease_hours: float = DEFAULT_LEASE_HOURS,
    override_overlap: bool = False,
    takeover_approved: bool = False,
    via_handoff: bool = False,
) -> tuple[int, str, dict]:
    """Claim a task. Returns (claim_id, classification, detail).

    Governance rules:
    - An open handoff reserves the task: normal claims are refused during the
      HANDOFF AVAILABLE interval. Only the accept path (via_handoff) proceeds.
    - BLOCKED (task held by an unexpired claim): refused unless the caller
      presents an approved takeover (checked by the caller via gate).
    - An expired holding claim is released here with an audit trail.
    - CONFLICTING (branch collision): refused; requires releasing/approval.
    - OVERLAPPING: refused unless explicitly overridden (recorded as such).
    """
    lock_task(conn, task_id)
    task = get_task(conn, task_id)
    agent_row = get_agent(conn, agent)
    if not scope_globs:
        raise ClaimRefused("At least one --scope is required to claim a task.")

    if not via_handoff:
        pending = open_handoff_for_task(conn, task_id)
        if pending:
            detail = {
                "reason": "reserved for open handoff",
                "handoff_id": pending["id"],
                "to_agent": pending["to_agent"],
            }
            conn.execute(
                """
                INSERT INTO conflict_events
                    (repository_id, task_id, agent_id, classification, detail)
                VALUES (%s, %s, %s, 'BLOCKED', %s)
                """,
                (task["repository_id"], task_id, agent_row["id"], Jsonb(detail)),
            )
            addressee = (
                f" (addressed to {pending['to_agent']})"
                if pending["to_agent"] else ""
            )
            raise ClaimRefused(
                f"Task {task_id} is reserved for open handoff "
                f"{pending['id']}{addressee}. Continue it with "
                f"`quorumgit handoff accept {pending['id']}`."
            )

    holder = active_claim_for_task(conn, task_id)
    displaced = None
    if holder:
        if holder["expired"]:
            release_claim(conn, holder["id"], agent="", reason="lease_expired",
                          enforce_owner=False)
        elif takeover_approved:
            # Released only after the replacement claim passes every rule:
            # a refused takeover must leave the holder untouched.
            displaced = holder
        # else: classification below reports BLOCKED

    classification, detail = classify(
        conn, task["repository_id"], task_id, branch, scope_globs,
        exclude_claim_id=displaced["id"] if displaced else None,
    )
    conn.execute(
        """
        INSERT INTO conflict_events
            (repository_id, task_id, agent_id, classification, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (task["repository_id"], task_id, agent_row["id"], classification,
         Jsonb(detail)),
    )

    if classification == "BLOCKED":
        raise ClaimRefused(
            f"Task {task_id} is already claimed by {detail['holder']} "
            f"(claim {detail['claim_id']}). Use a governed takeover."
        )
    if classification == "CONFLICTING":
        raise ClaimRefused(
            f"Branch {branch!r} is already claimed by "
            f"{', '.join(detail['holders'])}."
        )
    if classification == "OVERLAPPING" and not override_overlap:
        lines = [
            f"  {m['ours']} overlaps {m['theirs']} (held by {o['agent']})"
            for o in detail["overlaps"]
            for m in o["matches"]
        ]
        raise ClaimRefused(
            "Declared scopes overlap active work:\n"
            + "\n".join(lines)
            + "\nRe-run with --override-overlap to proceed as a recorded governance override."
        )

    if displaced:
        release_claim(conn, displaced["id"], agent=agent,
                      reason=f"takeover by {agent}", enforce_owner=False)

    row = conn.execute(
        """
        INSERT INTO claims (task_id, agent_id, branch, lease_expires_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s))
        RETURNING id
        """,
        (task_id, agent_row["id"], branch, lease_hours * 3600),
    ).fetchone()
    assert row is not None
    claim_id = row[0]
    for glob in scope_globs:
        conn.execute(
            "INSERT INTO scopes (claim_id, path_glob) VALUES (%s, %s)",
            (claim_id, glob),
        )
    set_task_status(conn, task_id, "claimed")
    audit.record(conn, "claim.acquired", "claim", claim_id, agent=agent,
                 detail={"task_id": task_id, "branch": branch,
                         "scopes": scope_globs,
                         "classification": classification,
                         "override_overlap": classification == "OVERLAPPING"})
    return claim_id, classification, detail


def renew_claim(
    conn: Connection, claim_id: int, agent: str,
    lease_hours: float = DEFAULT_LEASE_HOURS,
) -> None:
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorkError(f"Claim {claim_id} is already released.")
    if claim["agent"] != agent:
        raise WorkError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    conn.execute(
        """
        UPDATE claims SET lease_expires_at = now() + make_interval(secs => %s)
        WHERE id = %s
        """,
        (lease_hours * 3600, claim_id),
    )
    audit.record(conn, "claim.renewed", "claim", claim_id, agent=agent,
                 detail={"lease_hours": lease_hours})


def release_claim(
    conn: Connection, claim_id: int, agent: str, reason: str = "released",
    enforce_owner: bool = True,
) -> None:
    claim = get_claim(conn, claim_id)
    lock_task(conn, claim["task_id"])
    # Refresh after waiting for the task lock: another transaction may have
    # completed the ownership transition while this caller was blocked.
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorkError(f"Claim {claim_id} is already released.")
    if enforce_owner and claim["agent"] != agent:
        raise WorkError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    conn.execute(
        "UPDATE claims SET released_at = now(), release_reason = %s WHERE id = %s",
        (reason, claim_id),
    )
    set_task_status(conn, claim["task_id"], "open")
    audit.record(conn, "claim.released", "claim", claim_id,
                 agent=agent or None, detail={"reason": reason})


def add_checkpoint(
    conn: Connection, claim_id: int, agent: str, commit_oid: str,
    note: str = "", detail: dict | None = None,
) -> int:
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorkError(f"Claim {claim_id} is released; cannot checkpoint.")
    if claim["agent"] != agent:
        raise WorkError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    task = get_task(conn, claim["task_id"])
    verify_commit(task["repository_path"], commit_oid, branch=claim["branch"])
    row = conn.execute(
        """
        INSERT INTO checkpoints (claim_id, commit_oid, note, detail)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (claim_id, commit_oid, note, Jsonb(detail or {})),
    ).fetchone()
    assert row is not None
    audit.record(conn, "checkpoint.recorded", "claim", claim_id, agent=agent,
                 detail={"commit": commit_oid, "note": note})
    return row[0]
