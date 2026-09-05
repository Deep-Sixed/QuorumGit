"""Local libSQL connection contract for QuorumGit's next storage backend.

This module is intentionally isolated from the active PostgreSQL store until the
schema port lands. It establishes the runtime contract the port will depend on:
a local SQLite-compatible file, foreign-key enforcement, WAL journaling, a
bounded busy timeout, and explicit BEGIN IMMEDIATE governance transactions.
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Iterator

# libsql is a PyO3 extension whose published typing metadata does not expose
# runtime attributes such as connect(). Keep that third-party typing gap at
# this import boundary rather than weakening Pyright for the project.
libsql: Any = import_module("libsql")

DATABASE_FILENAME = "quorumgit.db"
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0


class LibsqlStoreError(RuntimeError):
    """Raised when the local libSQL runtime contract cannot be established."""


def database_path(data_dir: Path) -> Path:
    """Return the local QuorumGit database path for a state directory."""
    return data_dir / DATABASE_FILENAME


def connect_local(
    data_dir: Path, *, timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
) -> Any:
    """Open a local libSQL database with QuorumGit's required connection policy."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    data_dir.mkdir(parents=True, exist_ok=True)
    conn = libsql.connect(
        str(database_path(data_dir)),
        timeout=timeout_seconds,
        isolation_level=None,
    )

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")

        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        if foreign_keys != 1:
            raise LibsqlStoreError("libSQL connection did not enable foreign keys")
        if journal_mode != "wal":
            raise LibsqlStoreError(
                f"libSQL connection did not enter WAL mode: {journal_mode!r}"
            )
        if busy_timeout != int(timeout_seconds * 1000):
            raise LibsqlStoreError(
                "libSQL connection did not apply the requested busy timeout"
            )
    except Exception:
        conn.close()
        raise

    return conn


@contextmanager
def immediate_transaction(conn: Any) -> Iterator[Any]:
    """Serialize a governance write using SQLite/libSQL BEGIN IMMEDIATE."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
