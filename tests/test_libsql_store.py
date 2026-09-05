"""Contract tests for the staged local libSQL backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from quorumgit.libsql_store import (
    DEFAULT_BUSY_TIMEOUT_SECONDS,
    connect_local,
    database_path,
    immediate_transaction,
)

# Match the production boundary: libsql's runtime PyO3 exports are not fully
# represented in its published typing metadata.
libsql: Any = import_module("libsql")


def test_connect_local_establishes_required_pragmas(tmp_path):
    conn = connect_local(tmp_path)
    try:
        assert database_path(tmp_path).exists()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(
            DEFAULT_BUSY_TIMEOUT_SECONDS * 1000
        )
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = connect_local(tmp_path)
    try:
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child(parent_id INTEGER NOT NULL REFERENCES parent(id))"
        )
        with pytest.raises(libsql.Error):
            conn.execute("INSERT INTO child(parent_id) VALUES (1)")
    finally:
        conn.close()


def test_begin_immediate_commits_on_success(tmp_path):
    conn = connect_local(tmp_path)
    try:
        conn.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with immediate_transaction(conn):
            conn.execute("INSERT INTO items(value) VALUES (?)", ("committed",))
        assert conn.execute("SELECT value FROM items").fetchone()[0] == "committed"
    finally:
        conn.close()


def test_begin_immediate_rolls_back_on_failure(tmp_path):
    conn = connect_local(tmp_path)
    try:
        conn.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with pytest.raises(RuntimeError, match="stop"):
            with immediate_transaction(conn):
                conn.execute("INSERT INTO items(value) VALUES (?)", ("rolled-back",))
                raise RuntimeError("stop")
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()


def test_timeout_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="greater than zero"):
        connect_local(tmp_path, timeout_seconds=0)
