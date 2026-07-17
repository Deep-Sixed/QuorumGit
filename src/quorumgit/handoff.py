"""Durable, structured handoffs between agents.

A handoff carries everything the receiving agent needs to continue —
completed work, remaining work, last commit, files, blockers, validation —
so continuation never depends on reconstructing another agent's session.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from . import audit, trees, work
from .registry import get_agent


class HandoffError(RuntimeError):
    pass


def create_handoff(
    conn: Connection,
    claim_id: int,
    agent: str,
    record: dict[str, Any],
    to_agent: str | None = None,
) -> int:
    claim = work.get_claim(conn, claim_id)
    if claim["released_at"] is not None:
        raise HandoffError(f"Claim {claim_id} is released; nothing to hand off.")
    if claim["agent"] != agent:
        raise HandoffError(
            f"Claim {claim_id} belongs to {claim['agent']}, not {agent}."
        )
    for field in ("completed", "remaining", "last_commit"):
        if not record.get(field):
            raise HandoffError(f"Handoff record requires {field!r}.")
    task = work.get_task(conn, claim["task_id"])
    work.verify_commit(task["repository_path"], record["last_commit"],
                       branch=claim["branch"])

    to_agent_id = get_agent(conn, to_agent)["id"] if to_agent else None
    row = conn.execute(
        """
        INSERT INTO handoffs
            (task_id, from_claim_id, from_agent_id, to_agent_id, record)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (claim["task_id"], claim_id, claim["agent_id"], to_agent_id,
         Jsonb(record)),
    ).fetchone()
    assert row is not None
    handoff_id = row[0]

    # The claim is released; the worktree stays in place for the successor.
    work.release_claim(conn, claim_id, agent=agent, reason="handoff")
    work.set_task_status(conn, claim["task_id"], "handoff")
    audit.record(conn, "handoff.created", "handoff", handoff_id, agent=agent,
                 detail={"task_id": claim["task_id"], "to": to_agent,
                         "last_commit": record["last_commit"]})
    return handoff_id


def get_handoff(conn: Connection, handoff_id: int) -> dict:
    row = conn.execute(
        """
        SELECT h.id, h.task_id, h.from_claim_id, fa.name, ta.name, h.status,
               h.record
        FROM handoffs h
        JOIN agents fa ON fa.id = h.from_agent_id
        LEFT JOIN agents ta ON ta.id = h.to_agent_id
        WHERE h.id = %s
        """,
        (handoff_id,),
    ).fetchone()
    if row is None:
        raise HandoffError(f"No such handoff: {handoff_id}")
    return {
        "id": row[0], "task_id": row[1], "from_claim_id": row[2],
        "from_agent": row[3], "to_agent": row[4], "status": row[5],
        "record": row[6],
    }


def lock_handoff(conn: Connection, handoff_id: int) -> dict:
    """Lock a handoff before resolving it, then return its current state."""
    row = conn.execute(
        "SELECT id FROM handoffs WHERE id = %s FOR UPDATE",
        (handoff_id,),
    ).fetchone()
    if row is None:
        raise HandoffError(f"No such handoff: {handoff_id}")
    return get_handoff(conn, handoff_id)


def list_handoffs(conn: Connection, status: str | None = None) -> list[dict]:
    rows = conn.execute(
        """
        SELECT h.id, h.task_id, fa.name, ta.name, h.status
        FROM handoffs h
        JOIN agents fa ON fa.id = h.from_agent_id
        LEFT JOIN agents ta ON ta.id = h.to_agent_id
        WHERE (%s::text IS NULL OR h.status = %s)
        ORDER BY h.id
        """,
        (status, status),
    ).fetchall()
    return [
        {"id": r[0], "task_id": r[1], "from_agent": r[2], "to_agent": r[3],
         "status": r[4]}
        for r in rows
    ]


def accept_handoff(
    conn: Connection,
    handoff_id: int,
    agent: str,
    lease_hours: float = work.DEFAULT_LEASE_HOURS,
) -> dict:
    """Accept a handoff: new claim + transferred worktree, same branch."""
    handoff = lock_handoff(conn, handoff_id)
    if handoff["status"] != "open":
        raise HandoffError(f"Handoff {handoff_id} is {handoff['status']}.")
    if handoff["to_agent"] and handoff["to_agent"] != agent:
        raise HandoffError(
            f"Handoff {handoff_id} is addressed to {handoff['to_agent']}."
        )
    old_claim = work.get_claim(conn, handoff["from_claim_id"])
    old_scopes = [
        r[0] for r in conn.execute(
            "SELECT path_glob FROM scopes WHERE claim_id = %s",
            (handoff["from_claim_id"],),
        ).fetchall()
    ]

    claim_id, _, _ = work.claim_task(
        conn,
        handoff["task_id"],
        agent,
        branch=old_claim["branch"],
        scope_globs=old_scopes or ["**"],
        lease_hours=lease_hours,
        override_overlap=True,  # continuing declared work, not new overlap
        via_handoff=True,  # bypass the reservation this very handoff created
    )

    wt = trees.worktree_for_claim(conn, handoff["from_claim_id"])
    if wt and wt["removed_at"] is None:
        trees.transfer_worktree(conn, wt["id"], claim_id)

    cur = conn.execute(
        """
        UPDATE handoffs SET status = 'accepted', to_agent_id = %s,
               resolved_at = now()
        WHERE id = %s AND status = 'open'
        """,
        (get_agent(conn, agent)["id"], handoff_id),
    )
    if cur.rowcount != 1:
        raise HandoffError(
            f"Handoff {handoff_id} changed while it was being accepted."
        )
    audit.record(conn, "handoff.accepted", "handoff", handoff_id, agent=agent,
                 detail={"claim_id": claim_id,
                         "last_commit": handoff["record"]["last_commit"]})
    return {
        "claim_id": claim_id,
        "worktree": wt["path"] if wt else None,
        "branch": old_claim["branch"],
        "last_commit": handoff["record"]["last_commit"],
        "record": handoff["record"],
    }


def decline_handoff(conn: Connection, handoff_id: int, agent: str) -> None:
    """Decline an addressed handoff. Addressee only; the creator cancels
    instead, and non-addressees simply ignore an unaddressed handoff."""
    handoff = lock_handoff(conn, handoff_id)
    if handoff["status"] != "open":
        raise HandoffError(f"Handoff {handoff_id} is {handoff['status']}.")
    get_agent(conn, agent)  # registered identities only
    if handoff["to_agent"] is None:
        raise HandoffError(
            f"Handoff {handoff_id} is unaddressed; it cannot be declined. "
            "Its creator may cancel it."
        )
    if handoff["to_agent"] != agent:
        raise HandoffError(
            f"Handoff {handoff_id} is addressed to {handoff['to_agent']}; "
            f"only the addressee may decline it."
        )
    work.lock_task(conn, handoff["task_id"])
    cur = conn.execute(
        "UPDATE handoffs SET status = 'declined', resolved_at = now() "
        "WHERE id = %s AND status = 'open'",
        (handoff_id,),
    )
    if cur.rowcount != 1:
        raise HandoffError(
            f"Handoff {handoff_id} changed while it was being declined."
        )
    # The reservation ends: return the task to a defined claimable state.
    work.set_task_status(conn, handoff["task_id"], "open")
    audit.record(conn, "handoff.declined", "handoff", handoff_id, agent=agent)


def cancel_handoff(conn: Connection, handoff_id: int, agent: str) -> None:
    """Cancel an open handoff. Creator only."""
    handoff = lock_handoff(conn, handoff_id)
    if handoff["status"] != "open":
        raise HandoffError(f"Handoff {handoff_id} is {handoff['status']}.")
    get_agent(conn, agent)  # registered identities only
    if handoff["from_agent"] != agent:
        raise HandoffError(
            f"Handoff {handoff_id} was created by {handoff['from_agent']}; "
            f"only the creator may cancel it."
        )
    work.lock_task(conn, handoff["task_id"])
    cur = conn.execute(
        "UPDATE handoffs SET status = 'cancelled', resolved_at = now() "
        "WHERE id = %s AND status = 'open'",
        (handoff_id,),
    )
    if cur.rowcount != 1:
        raise HandoffError(
            f"Handoff {handoff_id} changed while it was being cancelled."
        )
    work.set_task_status(conn, handoff["task_id"], "open")
    audit.record(conn, "handoff.cancelled", "handoff", handoff_id, agent=agent)
