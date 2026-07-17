"""Append-only audit trail. Every state transition records an event in the
same transaction as the mutation it describes."""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


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
        VALUES (%s, %s, %s, %s, %s)
        """,
        (event_type, entity, entity_id, agent, Jsonb(detail or {})),
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
        WHERE (%s::text IS NULL OR entity = %s)
          AND (%s::bigint IS NULL OR entity_id = %s)
        ORDER BY id DESC
        LIMIT %s
    """
    rows = conn.execute(
        query, (entity, entity, entity_id, entity_id, limit)
    ).fetchall()
    keys = ("id", "event_type", "entity", "entity_id", "agent", "detail", "created_at")
    return [dict(zip(keys, row)) for row in rows]
