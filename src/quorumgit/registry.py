"""Registered repositories and agent identities."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import audit
from .store import Connection


class RegistryError(RuntimeError):
    pass


def _protected_refs(conn: Connection, repository_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT refname FROM protected_refs WHERE repository_id = ? ORDER BY id",
            (repository_id,),
        ).fetchall()
    ]


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
        INSERT INTO repositories (name, path)
        VALUES (?, ?)
        RETURNING id
        """,
        (name, str(repo_path)),
    ).fetchone()
    assert row is not None
    repo_id = row[0]
    for refname in protected_refs or []:
        conn.execute(
            "INSERT INTO protected_refs (repository_id, refname) VALUES (?, ?)",
            (repo_id, refname),
        )
    audit.record(
        conn,
        "repository.registered",
        "repository",
        repo_id,
        detail={"name": name, "path": str(repo_path)},
    )
    return repo_id


def get_repository(conn: Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name, path FROM repositories WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise RegistryError(f"Repository is not registered: {name}")
    return {
        "id": row[0],
        "name": row[1],
        "path": row[2],
        "protected_refs": _protected_refs(conn, row[0]),
    }


def list_repositories(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, path FROM repositories ORDER BY name"
    ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "path": r[2],
            "protected_refs": _protected_refs(conn, r[0]),
        }
        for r in rows
    ]


def add_agent(conn: Connection, name: str) -> int:
    row = conn.execute(
        "INSERT INTO agents (name) VALUES (?) RETURNING id", (name,)
    ).fetchone()
    assert row is not None
    agent_id = row[0]
    audit.record(conn, "agent.registered", "agent", agent_id, agent=name)
    return agent_id


def get_agent(conn: Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name FROM agents WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise RegistryError(f"Agent is not registered: {name}")
    return {"id": row[0], "name": row[1]}


def list_agents(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name FROM agents ORDER BY name").fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]
