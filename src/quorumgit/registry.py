"""Registered repositories and agent identities."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import audit
from .store import Connection, begin_immediate


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


def git_common_dir(path: str | Path) -> Path:
    """Return the canonical Git common directory for a repository path.

    Repository roots, subdirectories, and linked worktree paths can all name
    the same underlying Git repository. Governance identity is therefore bound
    to Git's common directory rather than to the user-supplied checkout path.
    """
    repo_path = Path(path).resolve()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RegistryError(f"Not a git repository: {repo_path}{suffix}")
    raw = result.stdout.strip()
    if not raw:
        raise RegistryError(f"Git returned no common directory for {repo_path}")
    common = Path(raw)
    if not common.is_absolute():
        common = repo_path / common
    return common.resolve()


def _identity_conflict(
    conn: Connection,
    common_dir: Path,
    *,
    exclude_repository_id: int | None = None,
) -> dict | None:
    rows = conn.execute(
        "SELECT id, name, path FROM repositories ORDER BY id"
    ).fetchall()
    for repository_id, name, path in rows:
        if exclude_repository_id is not None and repository_id == exclude_repository_id:
            continue
        try:
            existing_common = git_common_dir(path)
        except RegistryError:
            # A historical registration may outlive its checkout (for example
            # after an operator removes a repository or a test fixture is
            # cleaned up). It cannot prove an alias of the currently valid
            # target, so it must not wedge every later governance operation.
            # The target repository itself is still resolved strictly before
            # this scan by add_repository()/assert_repository_identity_unique().
            continue
        if existing_common == common_dir:
            return {
                "id": repository_id,
                "name": name,
                "path": path,
                "git_common_dir": str(existing_common),
            }
    return None


def assert_repository_identity_unique(
    conn: Connection,
    repository: dict,
) -> Path:
    """Return the repository common dir, failing closed on ambiguous identity."""
    common_dir = git_common_dir(repository["path"])
    conflict = _identity_conflict(
        conn,
        common_dir,
        exclude_repository_id=repository["id"],
    )
    if conflict is not None:
        raise RegistryError(
            f"Repository {repository['name']!r} shares Git common directory "
            f"{common_dir} with registered repository {conflict['name']!r}. "
            "Repository identity is ambiguous; remove the duplicate registration."
        )
    return common_dir


def add_repository(
    conn: Connection,
    name: str,
    path: str | Path,
    protected_refs: list[str] | None = None,
) -> int:
    begin_immediate(conn)
    repo_path = Path(path).resolve()
    common_dir = git_common_dir(repo_path)

    if conn.execute("SELECT 1 FROM repositories WHERE name = ?", (name,)).fetchone():
        raise RegistryError(f"Repository name is already registered: {name}")

    conflict = _identity_conflict(conn, common_dir)
    if conflict is not None:
        raise RegistryError(
            f"Git repository {common_dir} is already registered as "
            f"{conflict['name']!r} from {conflict['path']!r}."
        )

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
        detail={
            "name": name,
            "path": str(repo_path),
            "git_common_dir": str(common_dir),
        },
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
