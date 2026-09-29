"""QuorumGit's single persistent store: one local libSQL database file.

There is exactly one storage backend and one operational mode. The database is
owned by QuorumGit under QUORUMGIT_DATA_DIR; there is no PostgreSQL service,
external connection string, fallback store, or degraded mode.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
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

# libSQL 0.1.11's busy handler does not wake when a write lock is released: a
# contended BEGIN IMMEDIATE sleeps for the whole PRAGMA busy_timeout and then
# raises "database is locked", even if the holder committed immediately after
# the attempt began. A long busy_timeout therefore only delays a guaranteed
# failure. Instead each attempt fails fast and begin_immediate() polls until
# DEFAULT_BUSY_TIMEOUT_SECONDS, which does observe the release.
LOCK_POLL_TIMEOUT_MS = 50
LOCK_RETRY_INITIAL_SECONDS = 0.002
LOCK_RETRY_MAX_SECONDS = 0.05
MIGRATION_SEPARATOR = "-- quorumgit-statement"

# libsql 0.1.11 closes the underlying SQLite handle twice whenever a
# connection is torn down: dropping its inner LibsqlConnection calls
# sqlite3Close and frees the handle, then the outer Connection's drop calls
# sqlite3Close again on the freed memory (Valgrind: "Invalid read ... in
# sqlite3Close ... inside a block free'd by sqlite3Close"). It happens on
# explicit close() and on implicit garbage collection alike. Linux usually
# tolerates the stray read; Windows intermittently fails it with 0xC0000005.
# No newer libsql release exists, so short-lived CLI processes never tear a
# connection down: with retention enabled, release() keeps the connection
# alive and the process ends through os._exit (see cli.run). Every caller has
# committed or rolled back by then, so nothing is lost by skipping close.
_retain_connections = False
_retained: list[Any] = []


def retain_connections_for_process() -> None:
    """Keep released connections alive until the process exits (CLI only)."""
    global _retain_connections
    _retain_connections = True


def release(conn: Connection) -> None:
    """Close a connection, or retain it for the rest of a CLI process.

    A retained connection never keeps a transaction (or the write lock) open:
    anything still uncommitted is rolled back, as close() would have done.
    """
    if not _retain_connections:
        conn.close()
        return
    try:
        if conn.in_transaction:
            conn.rollback()
    finally:
        _retained.append(conn)

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
    "repository_approval_roles",
)

# Engine-level governance rules. A store missing any of these would still
# have every table, so the contract checks them by name as well.
REQUIRED_TRIGGERS = (
    "audit_events_no_update",
    "audit_events_no_delete",
    "approvals_require_registered_requester",
    "approvals_require_registered_consumer",
    "votes_require_registered_voter",
    "vote_updates_require_registered_voter",
    "repositories_default_approval_roles",
    "approvals_require_registered_repository",
    "votes_require_eligible_voter",
    "vote_updates_require_eligible_voter",
    "approvals_consumer_is_not_approver",
)


class StoreError(RuntimeError):
    pass


class ContractViolation(StoreError):
    pass


# ---------------------------------------------------------------- connection


def database_path(cfg: Config) -> Path:
    return cfg.data_dir / DATABASE_FILENAME


def _cfg_for_target(target: Config | str | Path) -> Config:
    if isinstance(target, Config):
        return target
    path = Path(target)
    return Config(data_dir=path.parent, agent=None)


def _configure_connection(conn: Connection, _timeout_seconds: float) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {LOCK_POLL_TIMEOUT_MS}")

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if foreign_keys != 1:
        raise ContractViolation("libSQL connection did not enable foreign keys")
    if journal_mode != "wal":
        raise ContractViolation(
            f"libSQL connection did not enter WAL mode: {journal_mode!r}"
        )
    if busy_timeout != LOCK_POLL_TIMEOUT_MS:
        raise ContractViolation("libSQL connection did not apply busy_timeout")


def open_connection(
    cfg: Config, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> Connection:
    """Open the local database without requiring an already-applied schema."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn: Connection | None = None
    try:
        conn = libsql.connect(
            str(database_path(cfg)),
            timeout=timeout_seconds,
            isolation_level="IMMEDIATE",
        )
        _configure_connection(conn, timeout_seconds)
        return conn
    except Exception as exc:
        if conn is not None:
            release(conn)
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
        release(conn)
        raise
    return conn


@contextmanager
def session(cfg: Config) -> Iterator[Connection]:
    """One command's connection: commit on success, roll back on error, release.

    libSQL's own context manager ends the transaction but never closes the
    connection; release() then closes it or, in a CLI process, retains it (see
    _retain_connections for why).
    """
    conn = connect(cfg)
    try:
        with conn:
            yield conn
    finally:
        release(conn)


def _is_locked_error(exc: Exception) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def begin_immediate(
    conn: Connection, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> None:
    """Acquire the single-writer reservation before governance reads.

    Polls rather than issuing one blocking BEGIN: see LOCK_POLL_TIMEOUT_MS for
    why libSQL's own busy handler cannot be relied on to wait here. Raises
    StoreError once timeout_seconds elapses so a contended governance command
    fails loudly instead of proceeding without the reservation.
    """
    if conn.in_transaction:
        return
    deadline = time.monotonic() + timeout_seconds
    delay = LOCK_RETRY_INITIAL_SECONDS
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except Exception as exc:
            if not _is_locked_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise StoreError(
                    "Could not acquire the store write lock within "
                    f"{timeout_seconds:g}s; another quorumgit command is "
                    "holding it. Retry once it finishes."
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, LOCK_RETRY_MAX_SECONDS)


# ---------------------------------------------------------------- lifecycle


def ensure_running(cfg: Config) -> str:
    """Compatibility name: ensure the local state directory exists.

    libSQL is embedded and has no server process to start. The returned string
    is the local database path retained for the pre-cutover CLI call shape.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return str(database_path(cfg))


def provision_extensions(_target: object) -> None:
    """No-op compatibility hook: libSQL requires no external extensions."""


def stop(_cfg: Config) -> None:
    """No-op compatibility hook: an embedded libSQL store has no daemon."""


def instance_status(cfg: Config) -> dict[str, Any]:
    path = database_path(cfg)
    exists = path.exists()
    return {
        "exists": exists,
        "path": str(path),
        # Compatibility keys consumed by the existing CLI until its cleanup PR.
        "running": exists,
        "uri": str(path) if exists else None,
    }


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


def migrate(target: Config | str | Path) -> list[str]:
    """Apply pending libSQL migrations in filename order."""
    cfg = _cfg_for_target(target)
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
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
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
        release(conn)


# ------------------------------------------------------------ contract check


def verify_contract(target: Config | Connection | str | Path) -> None:
    """Fail loudly unless the local store satisfies the runtime contract."""
    owned = isinstance(target, (Config, str, Path))
    if owned:
        cfg = _cfg_for_target(target)
        if not database_path(cfg).exists():
            raise StoreError("Store is not initialized. Run `quorumgit init` first.")
        conn = open_connection(cfg)
    else:
        conn = target

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "schema_migrations" not in tables:
            raise ContractViolation(
                "Missing required tables: ['schema_migrations']. "
                "Run `quorumgit init` to apply migrations."
            )

        required_migrations = {migration.name for migration in _migration_files()}
        applied_migrations = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        missing_migrations = required_migrations - applied_migrations
        if missing_migrations:
            raise ContractViolation(
                f"Missing required migrations: {sorted(missing_migrations)}. "
                "Run `quorumgit init` to apply migrations."
            )

        missing = set(REQUIRED_TABLES) - tables
        if missing:
            raise ContractViolation(
                f"Missing required tables: {sorted(missing)}. "
                "Run `quorumgit init` to apply migrations."
            )

        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        missing_triggers = set(REQUIRED_TRIGGERS) - triggers
        if missing_triggers:
            raise ContractViolation(
                f"Missing required governance triggers: {sorted(missing_triggers)}."
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
            release(conn)
