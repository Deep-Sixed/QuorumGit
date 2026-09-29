"""QuorumGit's single persistent store: one local SQLite 3 database file.

There is exactly one storage backend and one operational mode. The database is
owned by QuorumGit under QUORUMGIT_DATA_DIR and accessed through Python's
standard-library ``sqlite3`` module; there is no database server, external
connection string, fallback store, or degraded mode.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from .config import Config

Connection = sqlite3.Connection

DATABASE_FILENAME = "quorumgit.db"
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
# unixepoch() (used by the schema defaults) arrived in SQLite 3.38.0.
MINIMUM_SQLITE_VERSION = (3, 38, 0)
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
    "repository_approval_roles",
    "protected_paths",
    "allowed_ref_namespaces",
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
    "repositories_default_ref_namespaces",
)

# Kept byte-identical to the statement existing stores were created with.
SCHEMA_MIGRATIONS_DDL = """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL DEFAULT (unixepoch())
            )
            """


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


def _busy_timeout_ms(timeout_seconds: float) -> int:
    return max(1, round(timeout_seconds * 1000))


def _require_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
        required = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
        raise ContractViolation(
            f"SQLite {required} or newer is required; this Python is linked "
            f"against SQLite {sqlite3.sqlite_version}"
        )


def _configure_connection(conn: Connection, timeout_seconds: float) -> None:
    expected_busy_timeout = _busy_timeout_ms(timeout_seconds)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {expected_busy_timeout}")

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if foreign_keys != 1:
        raise ContractViolation("SQLite connection did not enable foreign keys")
    if journal_mode != "wal":
        raise ContractViolation(
            f"SQLite connection did not enter WAL mode: {journal_mode!r}"
        )
    if busy_timeout != expected_busy_timeout:
        raise ContractViolation("SQLite connection did not apply busy_timeout")


def open_connection(
    cfg: Config, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> Connection:
    """Open the local database without requiring an already-applied schema."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    _require_sqlite_version()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn: Connection | None = None
    try:
        conn = sqlite3.connect(
            str(database_path(cfg)),
            timeout=timeout_seconds,
            isolation_level="IMMEDIATE",
            # A connection may be handed to another thread (never shared
            # concurrently); SQLite's serialized threading mode permits this.
            check_same_thread=False,
        )
        _configure_connection(conn, timeout_seconds)
        return conn
    except Exception as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"Cannot open local SQLite store: {exc}") from exc


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


@contextmanager
def session(cfg: Config) -> Iterator[Connection]:
    """One command's connection: commit on success, roll back on error, close.

    sqlite3's own context manager ends the transaction but leaves the
    connection open until garbage collection; closing it here releases the
    database file as soon as the command is done with it.
    """
    conn = connect(cfg)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def begin_immediate(
    conn: Connection, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> None:
    """Acquire the single-writer reservation before governance reads.

    SQLite's busy handler waits up to timeout_seconds for a concurrent writer
    to release the lock. Raises StoreError once that elapses so a contended
    governance command fails loudly instead of proceeding without the
    reservation.
    """
    if conn.in_transaction:
        return
    previous_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    requested_ms = _busy_timeout_ms(timeout_seconds)
    if requested_ms != previous_ms:
        conn.execute(f"PRAGMA busy_timeout = {requested_ms}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if not _is_locked_error(exc):
            raise
        raise StoreError(
            "Could not acquire the store write lock within "
            f"{timeout_seconds:g}s; another quorumgit command is "
            "holding it. Retry once it finishes."
        ) from exc
    finally:
        if requested_ms != previous_ms:
            conn.execute(f"PRAGMA busy_timeout = {previous_ms}")


# ---------------------------------------------------------------- lifecycle


def ensure_running(cfg: Config) -> str:
    """Compatibility name: ensure the local state directory exists.

    SQLite is embedded and has no server process to start. The returned string
    is the local database path retained for the pre-cutover CLI call shape.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return str(database_path(cfg))


def provision_extensions(_target: object) -> None:
    """No-op compatibility hook: SQLite requires no external extensions."""


def stop(_cfg: Config) -> None:
    """No-op compatibility hook: an embedded SQLite store has no daemon."""


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
    """Apply pending SQLite migrations in filename order."""
    cfg = _cfg_for_target(target)
    conn = open_connection(cfg)
    applied: list[str] = []
    try:
        begin_immediate(conn)
        conn.execute(SCHEMA_MIGRATIONS_DDL)
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
        conn.close()


# ------------------------------------------------------------ contract check


def _normalized_sql(sql: str | None) -> str | None:
    """Schema SQL with formatting-only whitespace removed."""
    if sql is None:
        return None
    return re.sub(r" ?([(),;]) ?", r"\1", " ".join(sql.split()))


def _schema_objects(conn: Connection) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Every user schema object as {(type, name): (table, normalized SQL)}."""
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(row[0], row[1]): (row[2], _normalized_sql(row[3])) for row in rows}


@functools.lru_cache(maxsize=1)
def _reference_schema() -> dict[tuple[str, str], tuple[str, str | None]]:
    """The schema this QuorumGit build expects: every migration replayed in memory.

    Deriving the fingerprint from the migrations themselves keeps it exact
    without a hand-maintained list: each table, index (including the partial
    unique indexes that enforce single ownership and single live approvals)
    and trigger (including the append-only audit guards) must be present with
    the same definition.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        for mig in _migration_files():
            for statement in _migration_statements(mig.read_text(encoding="utf-8")):
                conn.execute(statement)
        return _schema_objects(conn)
    finally:
        conn.close()


def _describe(keys: set[tuple[str, str]]) -> str:
    return ", ".join(f"{kind} {name}" for kind, name in sorted(keys))


def _verify_schema_objects(conn: Connection) -> None:
    expected = _reference_schema()
    actual = _schema_objects(conn)
    missing = set(expected) - set(actual)
    changed = {
        key for key in set(expected) & set(actual) if expected[key] != actual[key]
    }
    # Extra tables are inert data; an extra index or trigger can change what
    # governance writes succeed or what they do, so it is a violation.
    unexpected = {
        key for key in set(actual) - set(expected) if key[0] != "table"
    }
    problems = []
    if missing:
        problems.append(f"missing {_describe(missing)}")
    if changed:
        problems.append(f"altered {_describe(changed)}")
    if unexpected:
        problems.append(f"unexpected {_describe(unexpected)}")
    if problems:
        raise ContractViolation(
            "Store schema does not match this QuorumGit version: "
            + "; ".join(problems)
            + ". Governance invariants cannot be guaranteed."
        )


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
        unknown_migrations = applied_migrations - required_migrations
        if unknown_migrations:
            raise ContractViolation(
                f"Store has migrations this QuorumGit does not know: "
                f"{sorted(unknown_migrations)}. It was written by a newer version."
            )
        _verify_schema_objects(conn)

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
            conn.close()
