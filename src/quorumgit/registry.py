"""Registered repositories and agent identities."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import audit
from .store import Connection, begin_immediate

ROLES = ("worker", "reviewer", "operator")
DEFAULT_ROLE = "worker"


class RegistryError(RuntimeError):
    pass


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise RegistryError(
            f"Unknown role {role!r}; expected one of: {', '.join(ROLES)}."
        )
    return role


def _operator_count(conn: Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM agents WHERE role = 'operator'"
    ).fetchone()
    assert row is not None
    return row[0]


def require_operator(conn: Connection, actor: str | None, action: str) -> None:
    """Refuse an administrative change unless an operator performs it.

    Roles and approval policy decide who may authorize protected operations,
    so changing them is itself an authority decision. Until the first operator
    exists there is nobody to ask, so the store is in bootstrap mode and the
    change is allowed (and audited with whatever actor was given).
    """
    if _operator_count(conn) == 0:
        return
    if not actor:
        raise RegistryError(
            f"{action} requires an operator; pass --agent or set QUORUMGIT_AGENT."
        )
    row = get_agent(conn, actor)
    if row["role"] != "operator":
        raise RegistryError(
            f"{action} requires an operator; {actor} is a {row['role']}."
        )


def _protected_refs(conn: Connection, repository_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT refname FROM protected_refs WHERE repository_id = ? ORDER BY id",
            (repository_id,),
        ).fetchall()
    ]


# Variables Git exports to hooks that select a repository. A lookup of a
# specific path must not inherit them, or every path resolves to the hook's
# repository.
_REPOSITORY_SELECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
)


def path_scoped_git_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _REPOSITORY_SELECTING_ENV
    }


def _protected_paths(conn: Connection, repository_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT path_glob FROM protected_paths WHERE repository_id = ? "
            "ORDER BY id",
            (repository_id,),
        ).fetchall()
    ]


def _allowed_ref_namespaces(conn: Connection, repository_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT prefix FROM allowed_ref_namespaces WHERE repository_id = ? "
            "ORDER BY prefix",
            (repository_id,),
        ).fetchall()
    ]


def _repository_dict(conn: Connection, row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "path": row[2],
        "protected_refs": _protected_refs(conn, row[0]),
        "protected_paths": _protected_paths(conn, row[0]),
        "allowed_ref_namespaces": _allowed_ref_namespaces(conn, row[0]),
    }


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
        env=path_scoped_git_env(),
        check=False,
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
    protected_paths: list[str] | None = None,
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
    for refname in dict.fromkeys(protected_refs or []):
        conn.execute(
            "INSERT INTO protected_refs (repository_id, refname) VALUES (?, ?)",
            (repo_id, refname),
        )
    for glob in protected_paths or []:
        conn.execute(
            "INSERT INTO protected_paths (repository_id, path_glob) VALUES (?, ?)",
            (repo_id, _validate_path_glob(glob)),
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
    return _repository_dict(conn, row)


def approval_policy(conn: Connection, repository_id: int) -> dict:
    """The repository-owned rule for who may authorize its operations."""
    row = conn.execute(
        "SELECT approval_threshold, requester_may_vote FROM repositories "
        "WHERE id = ?",
        (repository_id,),
    ).fetchone()
    if row is None:
        raise RegistryError(f"No such repository id: {repository_id}")
    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM repository_approval_roles "
            "WHERE repository_id = ? ORDER BY role",
            (repository_id,),
        ).fetchall()
    ]
    return {
        "threshold": row[0],
        "requester_may_vote": bool(row[1]),
        "roles": roles,
    }


def set_approval_policy(
    conn: Connection,
    repository: str,
    *,
    actor: str | None,
    threshold: int | None = None,
    roles: list[str] | None = None,
    requester_may_vote: bool | None = None,
) -> dict:
    begin_immediate(conn)
    repo = get_repository(conn, repository)
    require_operator(conn, actor, "Changing approval policy")
    before = approval_policy(conn, repo["id"])
    if threshold is not None:
        if threshold < 1:
            raise RegistryError("Approval threshold must be at least 1.")
        conn.execute(
            "UPDATE repositories SET approval_threshold = ? WHERE id = ?",
            (threshold, repo["id"]),
        )
    if roles is not None:
        wanted = sorted({_validate_role(role) for role in roles})
        if not wanted:
            raise RegistryError("At least one approving role is required.")
        conn.execute(
            "DELETE FROM repository_approval_roles WHERE repository_id = ?",
            (repo["id"],),
        )
        for role in wanted:
            conn.execute(
                "INSERT INTO repository_approval_roles (repository_id, role) "
                "VALUES (?, ?)",
                (repo["id"], role),
            )
    if requester_may_vote is not None:
        conn.execute(
            "UPDATE repositories SET requester_may_vote = ? WHERE id = ?",
            (1 if requester_may_vote else 0, repo["id"]),
        )
    after = approval_policy(conn, repo["id"])
    if after != before:
        audit.record(
            conn,
            "repository.policy_changed",
            "repository",
            repo["id"],
            agent=actor,
            detail={"before": before, "after": after},
        )
    return after


def _validate_path_glob(glob: str) -> str:
    if not glob.strip() or glob.startswith("-"):
        raise RegistryError(f"Invalid protected path glob: {glob!r}")
    return glob


def _validate_ref_namespace(prefix: str) -> str:
    if (
        not prefix.startswith("refs/")
        or not prefix.endswith("/")
        or len(prefix) <= len("refs//")
        or "//" in prefix
    ):
        raise RegistryError(
            f"Ref namespace must look like 'refs/<name>/', got {prefix!r}."
        )
    return prefix


def set_protected_path(
    conn: Connection,
    repository: str,
    glob: str,
    *,
    actor: str | None,
    remove: bool = False,
) -> list[str]:
    """Add or remove a path glob whose changes always need an approval."""
    begin_immediate(conn)
    repo = get_repository(conn, repository)
    require_operator(conn, actor, "Changing protected paths")
    _validate_path_glob(glob)
    if remove:
        cur = conn.execute(
            "DELETE FROM protected_paths WHERE repository_id = ? AND path_glob = ?",
            (repo["id"], glob),
        )
    else:
        cur = conn.execute(
            "INSERT INTO protected_paths (repository_id, path_glob) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            (repo["id"], glob),
        )
    if cur.rowcount:
        audit.record(
            conn,
            "repository.protected_path_removed" if remove
            else "repository.protected_path_added",
            "repository",
            repo["id"],
            agent=actor,
            detail={"path_glob": glob},
        )
    return _protected_paths(conn, repo["id"])


def set_allowed_ref_namespace(
    conn: Connection,
    repository: str,
    prefix: str,
    *,
    actor: str | None,
    remove: bool = False,
) -> list[str]:
    """Allow (or stop allowing) pushes to refs under a namespace prefix."""
    begin_immediate(conn)
    repo = get_repository(conn, repository)
    require_operator(conn, actor, "Changing allowed ref namespaces")
    _validate_ref_namespace(prefix)
    if remove:
        cur = conn.execute(
            "DELETE FROM allowed_ref_namespaces "
            "WHERE repository_id = ? AND prefix = ?",
            (repo["id"], prefix),
        )
    else:
        cur = conn.execute(
            "INSERT INTO allowed_ref_namespaces (repository_id, prefix) "
            "VALUES (?, ?) ON CONFLICT DO NOTHING",
            (repo["id"], prefix),
        )
    if cur.rowcount:
        audit.record(
            conn,
            "repository.ref_namespace_disallowed" if remove
            else "repository.ref_namespace_allowed",
            "repository",
            repo["id"],
            agent=actor,
            detail={"prefix": prefix},
        )
    return _allowed_ref_namespaces(conn, repo["id"])


def list_repositories(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, path FROM repositories ORDER BY name"
    ).fetchall()
    return [_repository_dict(conn, r) for r in rows]


def add_agent(
    conn: Connection,
    name: str,
    role: str = DEFAULT_ROLE,
    *,
    actor: str | None = None,
) -> int:
    _validate_role(role)
    begin_immediate(conn)
    if role != DEFAULT_ROLE:
        require_operator(conn, actor, f"Registering a {role}")
    if conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone():
        raise RegistryError(f"Agent is already registered: {name}")
    row = conn.execute(
        "INSERT INTO agents (name, role) VALUES (?, ?) RETURNING id", (name, role)
    ).fetchone()
    assert row is not None
    agent_id = row[0]
    detail = {"role": role}
    if actor:
        detail["registered_by"] = actor
    audit.record(
        conn, "agent.registered", "agent", agent_id, agent=name, detail=detail
    )
    return agent_id


def set_agent_role(
    conn: Connection, name: str, role: str, *, actor: str | None
) -> None:
    _validate_role(role)
    begin_immediate(conn)
    target = get_agent(conn, name)
    require_operator(conn, actor, "Changing an agent role")
    if target["role"] == role:
        return
    if target["role"] == "operator" and _operator_count(conn) == 1:
        # Demoting the last operator would drop the store back into bootstrap
        # mode, where any agent could then promote itself.
        raise RegistryError(
            f"{name} is the last operator; designate another operator first."
        )
    conn.execute("UPDATE agents SET role = ? WHERE id = ?", (role, target["id"]))
    audit.record(
        conn,
        "agent.role_changed",
        "agent",
        target["id"],
        agent=actor,
        detail={"agent": name, "from": target["role"], "to": role},
    )


def get_agent(conn: Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name, role FROM agents WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise RegistryError(f"Agent is not registered: {name}")
    return {"id": row[0], "name": row[1], "role": row[2]}


def list_agents(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, role FROM agents ORDER BY name"
    ).fetchall()
    return [{"id": r[0], "name": r[1], "role": r[2]} for r in rows]
