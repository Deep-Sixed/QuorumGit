"""Append-only audit trail. Every state transition records an event in the
same transaction as the mutation it describes."""

from __future__ import annotations

from typing import Any

from .store import Connection, json_dumps, json_loads


def record(
    conn: Connection,
    event_type: str,
    entity: str,
    entity_id: int | None = None,
    agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (event_type, entity, entity_id, agent, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, entity, entity_id, agent, json_dumps(detail or {})),
    )


def events(
    conn: Connection,
    entity: str | None = None,
    entity_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    query = """
        SELECT id, event_type, entity, entity_id, agent, detail, created_at
        FROM audit_events
        WHERE (? IS NULL OR entity = ?)
          AND (? IS NULL OR entity_id = ?)
        ORDER BY id DESC
        LIMIT ?
    """
    rows = conn.execute(
        query, (entity, entity, entity_id, entity_id, limit)
    ).fetchall()
    return [
        {
            "id": row[0],
            "event_type": row[1],
            "entity": row[2],
            "entity_id": row[3],
            "agent": row[4],
            "detail": json_loads(row[5], {}),
            "created_at": row[6],
        }
        for row in rows
    ]
