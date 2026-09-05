"""QuorumGit's single persistent store: one local libSQL database file.

There is exactly one storage backend and one operational mode. The database is
owned by QuorumGit under QUORUMGIT_DATA_DIR; there is no PostgreSQL service,
external connection string, fallback store, or degraded mode.
"""

from __future__ import annotations

import json
from importlib import import_module, resources
from pathlib import Path
from typing import Any

from .config import Config

# libsql is a PyO3 extension whose runtime exports are not fully represented in
# its published typing metadata. Keep the third-party typing gap at this one
# boundary rather than weakening Pyright for the project.
libsql: Any = import_module("libsql")
Connection = Any

DATABASE_FILENAME = "quorumgit.db"
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
MIGRATION_SEPARATOR = "-- quorumgit-statement"

REQUIRED_TABLES = (
    "schema_migrations",
    "repositories",
    "protected_refs",
    "agents",
    "tasks",
    "claims",
    "scopes",
    "worktrees",
    "checkpoints",
    "handoffs",
    "approvals",
    "votes",
    "conflict_events",
    "audit_events",
)


class StoreError(RuntimeError):
    pass


class ContractViolation(StoreError):
    pass


# ---------------------------------------------------------------- connection


def database_path(cfg: Config) -> Path:
    return cfg.data_dir / DATABASE_FILENAME


def _configure_connection(conn: Connection, timeout_seconds: float) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if foreign_keys != 1:
        raise ContractViolation("libSQL connection did not enable foreign keys")
    if journal_mode != "wal":
        raise ContractViolation(
            f"libSQL connection did not enter WAL mode: {journal_mode!r}"
        )
    if busy_timeout != int(timeout_seconds * 1000):
        raise ContractViolation("libSQL connection did not apply busy_timeout")


def open_connection(
    cfg: Config, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> Connection:
    """Open the local database without requiring an already-applied schema."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        conn = libsql.connect(
            str(database_path(cfg)),
            timeout=timeout_seconds,
            isolation_level="IMMEDIATE",
        )
        _configure_connection(conn, timeout_seconds)
        return conn
    except Exception as exc:
        try:
            conn.close()
        except (NameError, Exception):
            pass
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"Cannot open local libSQL store: {exc}") from exc


def connect(cfg: Config) -> Connection:
    """Open a normal connection and fail loudly unless the contract is valid."""
    if not database_path(cfg).exists():
        raise StoreError("Store is not initialized. Run `quorumgit init` first.")
    conn = open_connection(cfg)
    try:
        verify_contract(conn)
    except Exception:
        conn.close()
        raise
    return conn


def begin_immediate(conn: Connection) -> None:
    """Acquire the single-writer reservation before governance reads."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------- lifecycle


def instance_status(cfg: Config) -> dict[str, Any]:
    path = database_path(cfg)
    return {"exists": path.exists(), "path": str(path)}


def destroy(cfg: Config) -> None:
    """Delete the local store and SQLite sidecars. Irreversible."""
    path = database_path(cfg)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------- JSON


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(value: str | bytes | bytearray | None, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)


# ---------------------------------------------------------------- migrations


def _migration_files() -> list[Any]:
    return sorted(
        (
            f
            for f in resources.files("quorumgit.migrations").iterdir()
            if f.name.endswith(".sql")
        ),
        key=lambda f: f.name,
    )


def _migration_statements(text: str) -> list[str]:
    return [chunk.strip() for chunk in text.split(MIGRATION_SEPARATOR) if chunk.strip()]


def migrate(cfg: Config) -> list[str]:
    """Apply pending libSQL migrations in filename order."""
    conn = open_connection(cfg)
    applied: list[str] = []
    try:
        begin_immediate(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.commit()

        done = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        for mig in _migration_files():
            if mig.name in done:
                continue
            begin_immediate(conn)
            try:
                for statement in _migration_statements(
                    mig.read_text(encoding="utf-8")
                ):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (mig.name,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(mig.name)
        return applied
    except Exception as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"Migration failed: {exc}") from exc
    finally:
        conn.close()


# ------------------------------------------------------------ contract check


def verify_contract(target: Config | Connection) -> None:
    """Fail loudly unless the local store satisfies the runtime contract."""
    owned = isinstance(target, Config)
    if owned:
        if not database_path(target).exists():
            raise StoreError("Store is not initialized. Run `quorumgit init` first.")
        conn = open_connection(target)
    else:
        conn = target

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = set(REQUIRED_TABLES) - tables
        if missing:
            raise ContractViolation(
                f"Missing required tables: {sorted(missing)}. "
                "Run `quorumgit init` to apply migrations."
            )

        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if foreign_keys != 1:
            raise ContractViolation("foreign_keys is not enabled on this connection")
        if journal_mode != "wal":
            raise ContractViolation(f"journal_mode is {journal_mode!r}, expected 'wal'")

        json_ok = conn.execute("SELECT json_valid('{}')").fetchone()
        if json_ok is None or json_ok[0] != 1:
            raise ContractViolation("SQLite JSON functions are not functional")
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError(f"Store contract check failed: {exc}") from exc
    finally:
        if owned:
            conn.close()
