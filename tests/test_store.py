"""Store lifecycle, contract, persistence, and fail-loud behavior."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quorumgit import store
from quorumgit.canonical import HASH_PATTERN, canonical_json, stable_hash
from quorumgit.config import Config


def test_contract_ok(initialized_store):
    store.verify_contract(initialized_store)


def test_native_sqlite_contract_functions(conn):
    assert conn.execute("SELECT json_valid('{}')").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_migrations_idempotent(initialized_store):
    assert store.migrate(initialized_store) == []


def test_reopen_persists_data(cfg, initialized_store):
    connection = store.connect(initialized_store)
    connection.execute(
        "INSERT INTO agents (name) VALUES ('restart-probe') ON CONFLICT DO NOTHING"
    )
    connection.commit()
    connection.close()

    store.verify_contract(cfg)
    reopened = store.connect(cfg)
    try:
        row = reopened.execute(
            "SELECT 1 FROM agents WHERE name = 'restart-probe'"
        ).fetchone()
    finally:
        reopened.close()
    assert row is not None


def test_store_status_reports_local_file(cfg, initialized_store):
    status = store.instance_status(cfg)
    assert status["exists"] is True
    assert Path(status["path"]) == cfg.database_path


def test_incomplete_store_fails_loud(tmp_path):
    cfg = Config(data_dir=tmp_path / "broken", agent=None)
    conn = store.open_connection(cfg)
    conn.close()
    with pytest.raises(store.ContractViolation):
        store.verify_contract(cfg)


def test_begin_immediate_waiter_acquires_after_holder_releases(initialized_store):
    """A contended writer succeeds after release instead of timing out.

    libsql 0.1.11's native busy handler can otherwise sleep for the whole
    timeout and still raise after the holder has committed. This pins the
    polling behavior in store.begin_immediate().
    """
    holder = store.connect(initialized_store)
    waiter = store.connect(initialized_store)
    acquired = threading.Event()
    started = threading.Event()
    errors: list[Exception] = []
    elapsed: list[float] = []

    store.begin_immediate(holder)

    def contend() -> None:
        started.set()
        began = time.monotonic()
        try:
            store.begin_immediate(waiter, timeout_seconds=2.0)
            elapsed.append(time.monotonic() - began)
            acquired.set()
            waiter.rollback()
        except Exception as exc:
            errors.append(exc)

    try:
        thread = threading.Thread(target=contend)
        thread.start()
        assert started.wait(timeout=1)
        assert not acquired.wait(timeout=0.2), "waiter acquired before holder released"
        holder.commit()
        thread.join(timeout=3)
        assert not thread.is_alive(), "waiter did not return after holder released"
        assert not errors
        assert acquired.is_set()
        assert elapsed and elapsed[0] < 1.5
    finally:
        if holder.in_transaction:
            holder.rollback()
        if waiter.in_transaction:
            waiter.rollback()
        holder.close()
        waiter.close()


def test_single_store_no_external_mode():
    """The locked design: one local file store, no external-database mode."""
    from dataclasses import fields

    from quorumgit.config import Config as C

    names = {f.name for f in fields(C)}
    assert "database_url" not in names
    assert "pg_instance" not in names
    assert "pg_port" not in names
    assert "QUORUMGIT_DATABASE_URL" not in Path(store.__file__).read_text(
        encoding="utf-8"
    )


def test_canonical_hash_stable_and_order_independent():
    h1 = stable_hash({"b": 2, "a": 1})
    h2 = stable_hash({"a": 1, "b": 2})
    assert h1 == h2
    assert HASH_PATTERN.fullmatch(h1)
    assert canonical_json({"x": "é"}) == b'{"x":"\xc3\xa9"}'


def test_audit_append_only(conn):
    conn.execute(
        "INSERT INTO audit_events (event_type, entity) VALUES ('t', 'probe')"
    )
    with pytest.raises(ValueError, match="append-only"):
        conn.execute("DELETE FROM audit_events WHERE entity = 'probe'")


def test_session_commits_rolls_back_and_closes(tmp_path):
    """CLI sessions end their transaction and close the native connection."""
    local = Config(data_dir=tmp_path / "session", agent=None)
    store.migrate(local)
    try:
        # A closed libSQL connection panics on any further use, so closure
        # is not probed here; commit and rollback outcomes are.
        with store.session(local) as conn:
            conn.execute("INSERT INTO agents (name) VALUES ('kept')")

        with pytest.raises(RuntimeError):
            with store.session(local) as conn:
                conn.execute("INSERT INTO agents (name) VALUES ('dropped')")
                raise RuntimeError("abort")

        check = store.connect(local)
        try:
            names = {r[0] for r in check.execute("SELECT name FROM agents").fetchall()}
        finally:
            check.close()
        assert "kept" in names and "dropped" not in names
    finally:
        store.destroy(local)


def test_cli_process_retains_connections_instead_of_closing(tmp_path):
    """libsql 0.1.11 double-closes SQLite on teardown; the CLI never tears down.

    Runs in a subprocess because retention is process-wide by design.
    """
    local = Config(data_dir=tmp_path / "retain", agent=None)
    store.migrate(local)
    script = f"""
from pathlib import Path
from quorumgit import store
from quorumgit.config import Config
cfg = Config(data_dir=Path({str(local.data_dir)!r}), agent=None)
store.retain_connections_for_process()
with store.session(cfg) as conn:
    conn.execute("INSERT INTO agents (name) VALUES ('retained')")
assert conn.execute("SELECT 1").fetchone()[0] == 1, "connection was closed"
store.begin_immediate(conn)
conn.execute("INSERT INTO agents (name) VALUES ('never-committed')")
store.release(conn)
assert not conn.in_transaction, "retained connection kept its transaction"
import os
os._exit(0)  # as cli.run does: never let interpreter shutdown tear conn down
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        check = store.connect(local)
        try:
            names = {r[0] for r in check.execute("SELECT name FROM agents").fetchall()}
        finally:
            check.close()
        assert "retained" in names and "never-committed" not in names
    finally:
        store.destroy(local)
