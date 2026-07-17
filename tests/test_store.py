"""Phase 0: store lifecycle, contract, restart persistence, fail-loud."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from quorumgit import store
from quorumgit.canonical import HASH_PATTERN, canonical_json, stable_hash


def test_contract_ok(initialized_store):
    store.verify_contract(initialized_store)  # raises on violation


def test_extensions_functional(conn):
    ok = conn.execute(
        "SELECT jsonb_matches_schema('{\"type\":\"object\"}'::json, '{}'::jsonb)"
    ).fetchone()[0]
    assert ok is True
    digest = conn.execute("SELECT digest('x', 'sha256')").fetchone()[0]
    assert digest is not None


def test_migrations_idempotent(initialized_store):
    assert store.migrate(initialized_store) == []


def test_restart_persists_data(cfg, initialized_store):
    with psycopg.connect(initialized_store) as conn:
        conn.execute(
            "INSERT INTO agents (name) VALUES ('restart-probe') "
            "ON CONFLICT DO NOTHING"
        )
        conn.commit()
    store.stop(cfg)
    uri = store.ensure_running(cfg)
    store.verify_contract(uri)
    with psycopg.connect(uri) as conn:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = 'restart-probe'"
        ).fetchone()
    assert row is not None


def test_stopped_instance_reports_not_running(cfg, initialized_store):
    """A stopped instance reports running=False, not a bogus uri=None dict."""
    store.stop(cfg)
    try:
        status = store.instance_status(cfg)
        assert status["running"] is False
        assert "uri" not in status or status["uri"] is None
    finally:
        store.ensure_running(cfg)  # restore for the session-scoped store


def test_unreachable_store_fails_loud():
    with pytest.raises(store.StoreError):
        store.verify_contract(
            "postgresql://nobody:nothing@127.0.0.1:1/does_not_exist"
        )


def test_single_store_no_external_mode():
    """The locked design: one pg0 store, no external-database mode."""
    from dataclasses import fields

    from quorumgit.config import Config as C

    assert "database_url" not in {f.name for f in fields(C)}
    assert "QUORUMGIT_DATABASE_URL" not in Path(
        store.__file__
    ).read_text(encoding="utf-8")


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
    with pytest.raises(psycopg.Error):
        conn.execute("DELETE FROM audit_events WHERE entity = 'probe'")
