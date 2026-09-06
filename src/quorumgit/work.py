"""Tasks, claims, leases, declared scopes, and conflict classification.

Leases are timestamps, not timers: expiry is evaluated whenever a claim is
read, and an expired claim is explicitly released (with an audit event) at
the moment another operation supersedes it. No background process exists.
"""

from __future__ import annotations

import math
import subprocess
from typing import Any

from . import audit
from .registry import get_agent, get_repository
from .store import Connection, begin_immediate, json_dumps

DEFAULT_LEASE_HOURS = 8

CLASSIFICATIONS = ("CLEAR", "RELATED", "OVERLAPPING", "CONFLICTING", "BLOCKED")


class WorkError(RuntimeError):
    pass


class ClaimRefused(WorkError):
    """The claim was refused by governance rules; message says why."""


def _lease_seconds(lease_hours: float) -> int:
    if not math.isfinite(lease_hours) or lease_hours <= 0:
        raise WorkError("Lease duration must be finite and greater than zero.")
    seconds = int(lease_hours * 3600)
    if seconds <= 0:
        raise WorkError("Lease duration is too small; it must be at least one second.")
    return seconds


# ------------------------------------------------------------------- tasks


def create_task(
    conn: Connection, repository: str, title: str, objective: str = ""
) -> int:
    repo = get_repository(conn, repository)
    row = conn.execute(
        """
        INSERT INTO tasks (repository_id, title, objective)
        VALUES (?, ?, ?) RETURNING id
        """,
        (repo["id"], title, objective),
    ).fetchone()
    assert row is not None
    task_id = row[0]
    audit.record(
        conn,
        "task.created",
        "task",
        task_id,
        detail={"repository": repository, "title": title},
    )
    return task_id


def get_task(conn: Connection, task_id: int) -> dict:
    row = conn.execute(
        """
        SELECT t.id, t.repository_id, r.name, r.path, t.title, t.objective,
               t.status
        FROM tasks t JOIN repositories r ON r.id = t.repository_id
        WHERE t.id = ?
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
        WHERE (? IS NULL OR r.name = ?)
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
        "UPDATE tasks SET status = ?, updated_at = unixepoch() WHERE id = ?",
        (status, task_id),
    )


# ------------------------------------------------------------------- claims


def lock_task(conn: Connection, task_id: int) -> None:
    """Serialize ownership transitions before reading task state.

    libSQL inherits SQLite's single-writer model. BEGIN IMMEDIATE acquires the
    writer reservation for the whole governance transaction, replacing the
    PostgreSQL row lock while also serializing cross-task branch/scope checks.
    """
    begin_immediate(conn)
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise WorkError(f"No such task: {task_id}")


def _claim_row(row) -> dict:
    keys = (
        "id",
        "task_id",
        "agent_id",
        "agent",
        "branch",
        "lease_expires_at",
        "released_at",
        "release_reason",
        "expired",
    )
    return dict(zip(keys, row))


def get_claim(conn: Connection, claim_id: int) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.task_id, c.agent_id, a.name, c.branch,
               c.lease_expires_at, c.released_at, c.release_reason,
               (c.lease_expires_at < unixepoch()) AS expired
        FROM claims c JOIN agents a ON a.id = c.agent_id
        WHERE c.id = ?
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
               (c.lease_expires_at < unixepoch()) AS expired
        FROM claims c JOIN agents a ON a.id = c.agent_id
        WHERE c.task_id = ? AND c.released_at IS NULL
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
        SELECT c.id, c.task_id, a.name, c.branch
        FROM claims c
        JOIN tasks t ON t.id = c.task_id
        JOIN agents a ON a.id = c.agent_id
        WHERE t.repository_id = ?
          AND c.released_at IS NULL
          AND c.lease_expires_at >= unixepoch()
          AND (? IS NULL OR c.task_id <> ?)
        ORDER BY c.id
        """,
        (repository_id, exclude_task, exclude_task),
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        scopes = [
            r[0]
            for r in conn.execute(
                "SELECT path_glob FROM scopes WHERE claim_id = ? ORDER BY id",
                (row[0],),
            ).fetchall()
        ]
        result.append(
            {
                "id": row[0],
                "task_id": row[1],
                "agent": row[2],
                "branch": row[3],
                "scopes": scopes,
            }
        )
    return result


def open_handoff_for_task(conn: Connection, task_id: int) -> dict | None:
    """An open handoff reserving this task, if any."""
    row = conn.execute(
        """
        SELECT h.id, a.name
        FROM handoffs h
        LEFT JOIN agents a ON a.id = h.to_agent_id
        WHERE h.task_id = ? AND h.status = 'open'
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return {"id": row[0], "to_agent": row[1]} if row else None


def open_handoff_for_branch(
    conn: Connection,
    repository_id: int,
    branch: str,
    exclude_handoff_id: int | None = None,
) -> dict | None:
    """An open handoff that reserves this branch during the handoff gap."""
    row = conn.execute(
        """
        SELECT h.id
        FROM handoffs h
        JOIN claims c ON c.id = h.from_claim_id
        JOIN tasks t ON t.id = h.task_id
        WHERE t.repository_id = ? AND c.branch = ? AND h.status = 'open'
          AND (? IS NULL OR h.id <> ?)
        LIMIT 1
        """,
        (repository_id, branch, exclude_handoff_id, exclude_handoff_id),
    ).fetchone()
    return {"id": row[0]} if row else None


def open_handoff_scope_conflicts(
    conn: Connection,
    repository_id: int,
    scope_globs: list[str],
    exclude_handoff_id: int | None = None,
) -> list[dict]:
    """Return open handoff scope reservations overlapping prospective scopes."""
    rows = conn.execute(
        """
        SELECT h.id, s.path_glob
        FROM handoffs h
        JOIN claims c ON c.id = h.from_claim_id
        JOIN tasks t ON t.id = h.task_id
        JOIN scopes s ON s.claim_id = c.id
        WHERE t.repository_id = ? AND h.status = 'open'
          AND (? IS NULL OR h.id <> ?)
        ORDER BY h.id, s.id
        """,
        (repository_id, exclude_handoff_id, exclude_handoff_id),
    ).fetchall()
    conflicts: list[dict] = []
    for handoff_id, reserved in rows:
        for requested in scope_globs:
            if scopes_overlap(reserved, requested):
                conflicts.append(
                    {
                        "handoff_id": handoff_id,
                        "reserved": reserved,
                        "requested": requested,
                    }
                )
    return conflicts


def live_claim_for_branch(
    conn: Connection, repository_id: int, branch: str
) -> dict | None:
    row = conn.execute(
        """
        SELECT c.id, c.task_id, c.agent_id, a.name, c.branch,
               c.lease_expires_at, c.released_at, c.release_reason,
               (c.lease_expires_at < unixepoch()) AS expired
        FROM claims c
        JOIN tasks t ON t.id = c.task_id
        JOIN agents a ON a.id = c.agent_id
        WHERE t.repository_id = ? AND c.branch = ?
          AND c.released_at IS NULL AND c.lease_expires_at >= unixepoch()
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
    """Conservative overlap test that errs toward flagging."""
    pa, pb = _glob_prefix(a), _glob_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def verify_commit(
    repo_path: str, commit_oid: str, branch: str | None = None
) -> None:
    """Require an existing commit, reachable from the branch when it exists."""
    exists = (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "cat-file",
                "-e",
                f"{commit_oid}^{{commit}}",
            ],
            capture_output=True,
        ).returncode
        == 0
    )
    if not exists:
        raise WorkError(
            f"Commit {commit_oid} does not exist in the registered repository."
        )
    if branch:
        branch_exists = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                ],
                capture_output=True,
            ).returncode
            == 0
        )
        if branch_exists:
            reachable = (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_path),
                        "merge-base",
                        "--is-ancestor",
                        commit_oid,
                        f"refs/heads/{branch}",
                    ],
                    capture_output=True,
                ).returncode
                == 0
            )
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
    """Classify a prospective claim against all live claims in the repo."""
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
            overlaps.append(
                {
                    "claim_id": other["id"],
                    "agent": other["agent"],
                    "matches": hits,
                }
            )
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
    handoff_id: int | None = None,
) -> tuple[int, str, dict]:
    """Claim a task. Returns (claim_id, classification, detail)."""
    try:
        lease_seconds = _lease_seconds(lease_hours)
    except WorkError as exc:
        raise ClaimRefused(str(exc)) from exc
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
                VALUES (?, ?, ?, 'BLOCKED', ?)
                """,
                (
                    task["repository_id"],
                    task_id,
                    agent_row["id"],
                    json_dumps(detail),
                ),
            )
            addressee = (
                f" (addressed to {pending['to_agent']})"
                if pending["to_agent"]
                else ""
            )
            raise ClaimRefused(
                f"Task {task_id} is reserved for open handoff "
                f"{pending['id']}{addressee}. Continue it with "
                f"`quorumgit handoff accept {pending['id']}`."
            )

    excluded_handoff = handoff_id if via_handoff else None
    branch_reservation = open_handoff_for_branch(
        conn, task["repository_id"], branch, exclude_handoff_id=excluded_handoff
    )
    if branch_reservation:
        detail = {
            "reason": "branch reserved for open handoff",
            "handoff_id": branch_reservation["id"],
            "branch": branch,
        }
        conn.execute(
            """
            INSERT INTO conflict_events
                (repository_id, task_id, agent_id, classification, detail)
            VALUES (?, ?, ?, 'BLOCKED', ?)
            """,
            (
                task["repository_id"],
                task_id,
                agent_row["id"],
                json_dumps(detail),
            ),
        )
        raise ClaimRefused(
            f"Branch {branch!r} is reserved for open handoff "
            f"{branch_reservation['id']}."
        )

    handoff_scope_hits = open_handoff_scope_conflicts(
        conn,
        task["repository_id"],
        scope_globs,
        exclude_handoff_id=excluded_handoff,
    )
    if handoff_scope_hits:
        detail = {
            "reason": "scope reserved for open handoff",
            "overlaps": handoff_scope_hits,
        }
        conn.execute(
            """
            INSERT INTO conflict_events
                (repository_id, task_id, agent_id, classification, detail)
            VALUES (?, ?, ?, 'BLOCKED', ?)
            """,
            (
                task["repository_id"],
                task_id,
                agent_row["id"],
                json_dumps(detail),
            ),
        )
        ids = sorted({hit["handoff_id"] for hit in handoff_scope_hits})
        raise ClaimRefused(
            "Declared scopes are reserved by open handoff(s): "
            + ", ".join(str(i) for i in ids)
            + "."
        )

    holder = active_claim_for_task(conn, task_id)
    displaced = None
    if holder:
        if holder["expired"]:
            release_claim(
                conn,
                holder["id"],
                agent="",
                reason="lease_expired",
                enforce_owner=False,
            )
        elif takeover_approved:
            displaced = holder

    classification, detail = classify(
        conn,
        task["repository_id"],
        task_id,
        branch,
        scope_globs,
        exclude_claim_id=displaced["id"] if displaced else None,
    )
    conn.execute(
        """
        INSERT INTO conflict_events
            (repository_id, task_id, agent_id, classification, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            task["repository_id"],
            task_id,
            agent_row["id"],
            classification,
            json_dumps(detail),
        ),
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
        release_claim(
            conn,
            displaced["id"],
            agent=agent,
            reason=f"takeover by {agent}",
            enforce_owner=False,
        )

    row = conn.execute(
        """
        INSERT INTO claims (task_id, agent_id, branch, lease_expires_at)
        VALUES (?, ?, ?, unixepoch() + ?)
        RETURNING id
        """,
        (task_id, agent_row["id"], branch, lease_seconds),
    ).fetchone()
    assert row is not None
    claim_id = row[0]
    for glob in scope_globs:
        conn.execute(
            "INSERT INTO scopes (claim_id, path_glob) VALUES (?, ?)",
            (claim_id, glob),
        )
    set_task_status(conn, task_id, "claimed")
    audit.record(
        conn,
        "claim.acquired",
        "claim",
        claim_id,
        agent=agent,
        detail={
            "task_id": task_id,
            "branch": branch,
            "scopes": scope_globs,
            "classification": classification,
            "override_overlap": classification == "OVERLAPPING",
        },
    )
    return claim_id, classification, detail


def renew_claim(
    conn: Connection,
    claim_id: int,
    agent: str,
    lease_hours: float = DEFAULT_LEASE_HOURS,
) -> None:
    lease_seconds = _lease_seconds(lease_hours)
    begin_immediate(conn)
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorkError(f"Claim {claim_id} is already released.")
    if claim["agent"] != agent:
        raise WorkError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    if claim["expired"]:
        raise WorkError(
            f"Claim {claim_id} has expired and cannot be renewed; acquire a new claim."
        )
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() + ? WHERE id = ?",
        (lease_seconds, claim_id),
    )
    audit.record(
        conn,
        "claim.renewed",
        "claim",
        claim_id,
        agent=agent,
        detail={"lease_hours": lease_hours},
    )


def release_claim(
    conn: Connection,
    claim_id: int,
    agent: str,
    reason: str = "released",
    enforce_owner: bool = True,
) -> None:
    claim = get_claim(conn, claim_id)
    lock_task(conn, claim["task_id"])
    claim = get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise WorkError(f"Claim {claim_id} is already released.")
    if enforce_owner and claim["agent"] != agent:
        raise WorkError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    conn.execute(
        "UPDATE claims SET released_at = unixepoch(), release_reason = ? WHERE id = ?",
        (reason, claim_id),
    )
    set_task_status(conn, claim["task_id"], "open")
    audit.record(
        conn,
        "claim.released",
        "claim",
        claim_id,
        agent=agent or None,
        detail={"reason": reason},
    )


def add_checkpoint(
    conn: Connection,
    claim_id: int,
    agent: str,
    commit_oid: str,
    note: str = "",
    detail: dict | None = None,
) -> int:
    begin_immediate(conn)
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
        VALUES (?, ?, ?, ?) RETURNING id
        """,
        (claim_id, commit_oid, note, json_dumps(detail or {})),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        "checkpoint.recorded",
        "claim",
        claim_id,
        agent=agent,
        detail={"commit": commit_oid, "note": note},
    )
    return row[0]
