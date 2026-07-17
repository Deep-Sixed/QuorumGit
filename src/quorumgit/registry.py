"""Registered repositories and agent identities."""

from __future__ import annotations

import subprocess
from pathlib import Path

from psycopg import Connection

from . import audit


class RegistryError(RuntimeError):
    pass


def add_repository(
    conn: Connection,
    name: str,
    path: str | Path,
    protected_refs: list[str] | None = None,
) -> int:
    repo_path = Path(path).resolve()
    git_check = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if git_check.returncode != 0:
        raise RegistryError(f"Not a git repository: {repo_path}")

    row = conn.execute(
        """
        INSERT INTO repositories (name, path, protected_refs)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (name, str(repo_path), protected_refs or []),
    ).fetchone()
    assert row is not None
    repo_id = row[0]
    audit.record(conn, "repository.registered", "repository", repo_id,
                 detail={"name": name, "path": str(repo_path)})
    return repo_id


def get_repository(conn: Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name, path, protected_refs FROM repositories WHERE name = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise RegistryError(f"Repository is not registered: {name}")
    return {"id": row[0], "name": row[1], "path": row[2], "protected_refs": row[3]}


def list_repositories(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, path, protected_refs FROM repositories ORDER BY name"
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "path": r[2], "protected_refs": r[3]}
        for r in rows
    ]


def add_agent(conn: Connection, name: str) -> int:
    row = conn.execute(
        "INSERT INTO agents (name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()
    assert row is not None
    agent_id = row[0]
    audit.record(conn, "agent.registered", "agent", agent_id, agent=name)
    return agent_id


def get_agent(conn: Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name FROM agents WHERE name = %s", (name,)
    ).fetchone()
    if row is None:
        raise RegistryError(f"Agent is not registered: {name}")
    return {"id": row[0], "name": row[1]}


def list_agents(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name FROM agents ORDER BY name").fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]
