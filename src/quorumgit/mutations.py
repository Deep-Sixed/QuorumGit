"""Durable reservations for Git ref mutations in flight.

`pre-receive` validates governance and reserves the exact ref transition before
Git performs it. Governance changes that could invalidate that decision are
blocked while the reservation is live. `post-receive` completes the same
reservation only after Git reports the ref update as successful.
"""

from __future__ import annotations

from typing import Any

from . import audit
from .canonical import stable_hash
from .store import Connection, begin_immediate

DEFAULT_RESERVATION_SECONDS = 300


class MutationError(RuntimeError):
    pass


def mutation_payload(
    repository: str,
    refname: str,
    oldrev: str,
    newrev: str,
) -> dict[str, str]:
    return {
        "type": "git_ref_mutation",
        "repository": repository,
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
    }


def mutation_hash(
    repository: str,
    refname: str,
    oldrev: str,
    newrev: str,
) -> str:
    return stable_hash(mutation_payload(repository, refname, oldrev, newrev))


def _row_dict(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "repository_id": row[1],
        "refname": row[2],
        "oldrev": row[3],
        "newrev": row[4],
        "mutation_hash": row[5],
        "agent_id": row[6],
        "approval_id": row[7],
        "status": row[8],
        "created_at": row[9],
        "expires_at": row[10],
        "completed_at": row[11],
    }


def expire_stale(conn: Connection, repository_id: int | None = None) -> int:
    """Expire abandoned reservations and audit each transition.

    A failed receive can exit after pre-receive without a completion callback.
    Reservations are therefore leases rather than permanent locks. Expiry does
    not consume an approval and does not claim that Git changed the ref.
    """
    begin_immediate(conn)
    params: tuple[Any, ...]
    if repository_id is None:
        rows = conn.execute(
            """
            SELECT id FROM git_mutations
            WHERE status = 'reserved' AND expires_at < unixepoch()
            ORDER BY id
            """
        ).fetchall()
        params = ()
        where = "status = 'reserved' AND expires_at < unixepoch()"
    else:
        rows = conn.execute(
            """
            SELECT id FROM git_mutations
            WHERE repository_id = ? AND status = 'reserved'
              AND expires_at < unixepoch()
            ORDER BY id
            """,
            (repository_id,),
        ).fetchall()
        params = (repository_id,)
        where = (
            "repository_id = ? AND status = 'reserved' "
            "AND expires_at < unixepoch()"
        )
    if not rows:
        return 0
    conn.execute(
        f"UPDATE git_mutations SET status = 'expired' WHERE {where}",  # noqa: S608 -- fixed clauses only
        params,
    )
    for row in rows:
        audit.record(
            conn,
            "git_mutation.expired",
            "git_mutation",
            row[0],
            detail={"reason": "reservation_timeout"},
        )
    return len(rows)


def active_for_ref(
    conn: Connection,
    repository_id: int,
    refname: str,
) -> dict[str, Any] | None:
    expire_stale(conn, repository_id)
    row = conn.execute(
        """
        SELECT id, repository_id, refname, oldrev, newrev, mutation_hash,
               agent_id, approval_id, status, created_at, expires_at, completed_at
        FROM git_mutations
        WHERE repository_id = ? AND refname = ? AND status = 'reserved'
        LIMIT 1
        """,
        (repository_id, refname),
    ).fetchone()
    return _row_dict(row) if row else None


def assert_ref_available(
    conn: Connection,
    repository_id: int,
    refname: str,
    *,
    allow_mutation_id: int | None = None,
) -> None:
    current = active_for_ref(conn, repository_id, refname)
    if current is None or current["id"] == allow_mutation_id:
        return
    raise MutationError(
        f"Git ref {refname!r} has in-flight mutation {current['id']} "
        f"({current['oldrev']} -> {current['newrev']}); retry after it completes "
        "or its reservation expires."
    )


def assert_branch_available(
    conn: Connection,
    repository_id: int,
    branch: str,
) -> None:
    assert_ref_available(conn, repository_id, f"refs/heads/{branch}")


def reserve(
    conn: Connection,
    *,
    repository_id: int,
    repository: str,
    refname: str,
    oldrev: str,
    newrev: str,
    agent_id: int,
    agent: str,
    approval_id: int | None = None,
    reservation_seconds: int = DEFAULT_RESERVATION_SECONDS,
) -> dict[str, Any]:
    """Reserve one exact ref transition.

    Retries of the identical mutation by the same agent and approval instance
    renew the existing lease; a different in-flight mutation on the same ref is
    rejected. The approval remains merely approved until Git actually succeeds.
    """
    if reservation_seconds <= 0:
        raise MutationError("Git mutation reservation duration must be positive.")
    begin_immediate(conn)
    expire_stale(conn, repository_id)
    digest = mutation_hash(repository, refname, oldrev, newrev)
    current = active_for_ref(conn, repository_id, refname)
    if current is not None:
        if (
            current["mutation_hash"] == digest
            and current["agent_id"] == agent_id
            and current["approval_id"] == approval_id
        ):
            conn.execute(
                "UPDATE git_mutations SET expires_at = unixepoch() + ? WHERE id = ?",
                (reservation_seconds, current["id"]),
            )
            current["expires_at"] = None
            audit.record(
                conn,
                "git_mutation.renewed",
                "git_mutation",
                current["id"],
                agent=agent,
                detail={"mutation_hash": digest, "refname": refname},
            )
            return current
        raise MutationError(
            f"Git ref {refname!r} already has in-flight mutation {current['id']}."
        )

    row = conn.execute(
        """
        INSERT INTO git_mutations (
            repository_id, refname, oldrev, newrev, mutation_hash,
            agent_id, approval_id, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch() + ?)
        RETURNING id, repository_id, refname, oldrev, newrev, mutation_hash,
                  agent_id, approval_id, status, created_at, expires_at, completed_at
        """,
        (
            repository_id,
            refname,
            oldrev,
            newrev,
            digest,
            agent_id,
            approval_id,
            reservation_seconds,
        ),
    ).fetchone()
    assert row is not None
    result = _row_dict(row)
    audit.record(
        conn,
        "git_mutation.reserved",
        "git_mutation",
        result["id"],
        agent=agent,
        detail={
            "repository": repository,
            "refname": refname,
            "oldrev": oldrev,
            "newrev": newrev,
            "mutation_hash": digest,
            "approval_id": approval_id,
        },
    )
    return result


def reserved_exact(
    conn: Connection,
    *,
    repository_id: int,
    repository: str,
    refname: str,
    oldrev: str,
    newrev: str,
    agent_id: int,
) -> dict[str, Any] | None:
    expire_stale(conn, repository_id)
    digest = mutation_hash(repository, refname, oldrev, newrev)
    row = conn.execute(
        """
        SELECT id, repository_id, refname, oldrev, newrev, mutation_hash,
               agent_id, approval_id, status, created_at, expires_at, completed_at
        FROM git_mutations
        WHERE repository_id = ? AND mutation_hash = ? AND agent_id = ?
          AND status = 'reserved'
        LIMIT 1
        """,
        (repository_id, digest, agent_id),
    ).fetchone()
    return _row_dict(row) if row else None


def complete(conn: Connection, mutation_id: int, *, agent: str) -> None:
    begin_immediate(conn)
    cur = conn.execute(
        """
        UPDATE git_mutations
        SET status = 'completed', completed_at = unixepoch()
        WHERE id = ? AND status = 'reserved'
        """,
        (mutation_id,),
    )
    if cur.rowcount != 1:
        raise MutationError(
            f"Git mutation {mutation_id} is not an active reservation."
        )
    audit.record(
        conn,
        "git_mutation.completed",
        "git_mutation",
        mutation_id,
        agent=agent,
    )
