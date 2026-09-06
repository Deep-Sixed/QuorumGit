"""Regression coverage for registered identity and approval-instance binding."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, registry, store, work
from quorumgit.config import Config
from quorumgit.registry import RegistryError
from tests.conftest import make_git_repo
from tests.test_cli_hub import _cli
from tests.test_gate import _commit, _push, _setup


def _agents(conn, *names):
    for name in names:
        conn.execute("INSERT INTO agents (name) VALUES (?) ON CONFLICT DO NOTHING", (name,))


def _apply_001(cfg: Config):
    conn = store.open_connection(cfg)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at INTEGER NOT NULL DEFAULT (unixepoch()))"
    )
    migration = Path(store.__file__).parent / "migrations" / "001_core.sql"
    for statement in store._migration_statements(migration.read_text(encoding="utf-8")):
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations (version) VALUES ('001_core.sql')")
    return conn


def _migrate_after_001(cfg: Config) -> list[str]:
    """Apply every packaged successor while keeping the identity migration first."""
    applied = store.migrate(cfg)
    assert applied and applied[0] == "002_approval_identities.sql"
    return applied


def _register_001_agent(conn, name: str) -> int:
    row = conn.execute(
        "INSERT INTO agents (name) VALUES (?) RETURNING id", (name,)
    ).fetchone()
    assert row is not None
    agent_id = row[0]
    conn.execute(
        "INSERT INTO audit_events (event_type, entity, entity_id, agent) "
        "VALUES ('agent.registered', 'agent', ?, ?)",
        (agent_id, name),
    )
    return agent_id


def _insert_001_approval(conn, operation: dict, requester: str) -> int:
    row = conn.execute(
        "INSERT INTO approvals (operation_hash, operation, status) "
        "VALUES (?, ?, 'approved') RETURNING id",
        (gate.operation_hash(operation), store.json_dumps(operation)),
    ).fetchone()
    assert row is not None
    approval_id = row[0]
    conn.execute(
        "INSERT INTO audit_events (event_type, entity, entity_id, agent) "
        "VALUES ('approval.requested', 'approval', ?, ?)",
        (approval_id, requester),
    )
    return approval_id


def _insert_001_vote(conn, approval_id: int, voter: str) -> None:
    conn.execute(
        "INSERT INTO votes (approval_id, voter, vote) VALUES (?, ?, 1)",
        (approval_id, voter),
    )
    conn.execute(
        "INSERT INTO audit_events (event_type, entity, entity_id, agent) "
        "VALUES ('approval.vote', 'approval', ?, ?)",
        (approval_id, voter),
    )


def test_approval_requester_must_be_registered(conn):
    operation = {"type": "test", "repository": "r"}
    before = conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
    with pytest.raises(RegistryError, match="not registered"):
        gate.request_approval(conn, operation, requested_by="ghost")
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == before


def test_approval_voter_must_be_registered(conn):
    _agents(conn, "requester")
    operation = {"type": "test", "repository": "r"}
    approval = gate.request_approval(conn, operation, requested_by="requester")
    before = conn.execute("SELECT count(*) FROM votes").fetchone()[0]

    with pytest.raises(RegistryError, match="not registered"):
        gate.vote(conn, approval["id"], "ghost", True)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM votes").fetchone()[0] == before


def test_approval_consumer_must_be_registered(conn):
    _agents(conn, "requester", "voter")
    operation = {"type": "test", "repository": "r"}
    approval = gate.request_approval(conn, operation, requested_by="requester")
    gate.vote(conn, approval["id"], "voter", True)

    with pytest.raises(RegistryError, match="not registered"):
        gate.consume_approval(conn, approval["id"], operation, agent="ghost")
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"


def test_approval_consumer_must_present_matching_instance_and_operation(conn):
    _agents(conn, "requester", "voter", "consumer")
    first_operation = {"type": "first", "repository": "r"}
    second_operation = {"type": "second", "repository": "r"}
    first = gate.request_approval(conn, first_operation, requested_by="requester")
    second = gate.request_approval(conn, second_operation, requested_by="requester")
    gate.vote(conn, first["id"], "voter", True)
    gate.vote(conn, second["id"], "voter", True)

    with pytest.raises(gate.GateError, match="not bound"):
        gate.consume_approval(conn, first["id"], second_operation, agent="consumer")
    assert gate.get_approval_by_id(conn, first["id"])["status"] == "approved"
    assert gate.get_approval_by_id(conn, second["id"])["status"] == "approved"


def test_delayed_vote_cannot_target_new_approval_instance(conn):
    _agents(conn, "requester", "first-voter", "late-voter")
    operation = {"type": "repeat", "repository": "r"}
    first = gate.request_approval(conn, operation, requested_by="requester")
    gate.vote(conn, first["id"], "first-voter", False)
    second = gate.request_approval(conn, operation, requested_by="requester")

    with pytest.raises(gate.GateError, match="already denied"):
        gate.vote(conn, first["id"], "late-voter", True)
    assert gate.get_approval_by_id(conn, second["id"])["status"] == "pending"


def test_cli_votes_by_approval_instance_id(committed_conn, cfg):
    _agents(committed_conn, "cli-requester", "cli-voter")
    committed_conn.commit()
    operation = {"type": "cli", "repository": "r", "nonce": uuid.uuid4().hex}
    requested = _cli(
        cfg,
        "approve",
        "request",
        json.dumps(operation),
        agent="cli-requester",
    )
    assert requested.returncode == 0, requested.stderr
    match = re.search(r"approval (\d+) hash=", requested.stdout)
    assert match is not None

    voted = _cli(
        cfg,
        "approve",
        "vote",
        match.group(1),
        agent="cli-voter",
    )
    assert voted.returncode == 0, voted.stderr
    assert "status=approved" in voted.stdout


def test_new_approval_rows_reference_registered_agents(conn):
    _agents(conn, "requester", "voter", "consumer")
    operation = {"type": "identity-columns", "repository": "r"}
    approval = gate.request_approval(conn, operation, requested_by="requester")
    gate.vote(conn, approval["id"], "voter", True)
    gate.consume_approval(conn, approval["id"], operation, agent="consumer")

    row = conn.execute(
        """
        SELECT a.requested_by_agent_id, a.consumed_by_agent_id, v.voter_agent_id
        FROM approvals a JOIN votes v ON v.approval_id = a.id
        WHERE a.id = ?
        """,
        (approval["id"],),
    ).fetchone()
    assert row is not None and all(value is not None for value in row)


def test_contract_rejects_001_only_store_until_all_migrations_apply(tmp_path):
    cfg = Config(data_dir=tmp_path / "only-001", agent=None)
    conn = _apply_001(cfg)
    conn.commit()
    conn.close()

    for opener in (store.verify_contract, store.connect):
        with pytest.raises(store.ContractViolation) as caught:
            opener(cfg)
        message = str(caught.value)
        assert "002_approval_identities.sql" in message
        assert "003_git_mutations.sql" in message
        assert "Run `quorumgit init`" in message

    applied = _migrate_after_001(cfg)
    assert "003_git_mutations.sql" in applied
    store.verify_contract(cfg)
    upgraded = store.connect(cfg)
    upgraded.close()


def test_identity_migration_preserves_and_backfills_history(tmp_path):
    cfg = Config(data_dir=tmp_path / "historical", agent=None)
    conn = _apply_001(cfg)
    _register_001_agent(conn, "known")
    operation = {"type": "historical", "repository": "r"}
    approval_id = _insert_001_approval(conn, operation, "known")
    _insert_001_vote(conn, approval_id, "known")
    _insert_001_vote(conn, approval_id, "legacy-ghost")
    conn.commit()
    conn.close()

    applied = _migrate_after_001(cfg)
    assert "003_git_mutations.sql" in applied
    migrated = store.connect(cfg)
    try:
        requester = migrated.execute(
            "SELECT requested_by_agent_id, status FROM approvals WHERE id = 1"
        ).fetchone()
        voters = migrated.execute(
            "SELECT voter, voter_agent_id FROM votes ORDER BY id"
        ).fetchall()
        assert requester is not None and requester[0] is not None
        assert requester[1] == "denied"
        assert voters[0][1] is not None
        assert voters[1][1] is None
        invalidation = migrated.execute(
            "SELECT detail FROM audit_events "
            "WHERE event_type = 'approval.identity_invalidated' AND entity_id = 1"
        ).fetchone()
        assert invalidation is not None
        assert store.json_loads(invalidation[0], {})["reason"] == (
            "unregistered historical voter"
        )
        with pytest.raises(ValueError, match="registered agent"):
            migrated.execute(
                "INSERT INTO approvals (operation_hash, operation) VALUES (?, ?)",
                ("sha256:" + "1" * 64, store.json_dumps(operation)),
            )
    finally:
        migrated.rollback()
        migrated.close()


def test_migration_does_not_backfill_requester_registered_after_request(tmp_path):
    cfg = Config(data_dir=tmp_path / "late-requester", agent=None)
    conn = _apply_001(cfg)
    operation = {"type": "late-requester", "repository": "r"}
    approval_id = _insert_001_approval(conn, operation, "ghost")
    _register_001_agent(conn, "ghost")
    conn.commit()
    conn.close()

    applied = _migrate_after_001(cfg)
    assert "003_git_mutations.sql" in applied
    migrated = store.connect(cfg)
    try:
        row = migrated.execute(
            "SELECT requested_by_agent_id, status FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        assert row == (None, "denied")
        event = migrated.execute(
            "SELECT detail FROM audit_events "
            "WHERE event_type = 'approval.identity_invalidated' AND entity_id = ?",
            (approval_id,),
        ).fetchone()
        assert event is not None
        assert store.json_loads(event[0], {})["reason"] == (
            "unregistered historical requester"
        )
    finally:
        migrated.close()


def test_migration_does_not_backfill_voter_registered_after_vote(tmp_path):
    cfg = Config(data_dir=tmp_path / "late-voter", agent=None)
    conn = _apply_001(cfg)
    _register_001_agent(conn, "requester")
    operation = {"type": "late-voter", "repository": "r"}
    approval_id = _insert_001_approval(conn, operation, "requester")
    _insert_001_vote(conn, approval_id, "ghost")
    _register_001_agent(conn, "ghost")
    conn.commit()
    conn.close()

    applied = _migrate_after_001(cfg)
    assert "003_git_mutations.sql" in applied
    migrated = store.connect(cfg)
    try:
        approval = migrated.execute(
            "SELECT requested_by_agent_id, status FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        vote = migrated.execute(
            "SELECT voter_agent_id FROM votes WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        assert approval is not None and approval[0] is not None
        assert approval[1] == "denied"
        assert vote == (None,)
        event = migrated.execute(
            "SELECT detail FROM audit_events "
            "WHERE event_type = 'approval.identity_invalidated' AND entity_id = ?",
            (approval_id,),
        ).fetchone()
        assert event is not None
        assert store.json_loads(event[0], {})["reason"] == (
            "unregistered historical voter"
        )
    finally:
        migrated.close()


def test_stale_takeover_approval_cannot_displace_reacquired_claim(
    committed_conn, cfg, tmp_path
):
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]
    repository = f"stale-{suffix}"
    repo_path = make_git_repo(tmp_path / "repo")
    registry.add_repository(conn, repository, repo_path)
    owner, successor, operator = (
        f"owner-{suffix}",
        f"successor-{suffix}",
        f"operator-{suffix}",
    )
    _agents(conn, owner, successor, operator)
    task = work.create_task(conn, repository, "stale takeover")
    old_claim, _, _ = work.claim_task(
        conn, task, owner, "feat/old", ["src/**"]
    )
    operation = {
        "type": "lease_takeover",
        "repository": repository,
        "task_id": task,
        "from_claim_id": old_claim,
        "from_agent": owner,
        "to_agent": successor,
    }
    approval = gate.request_approval(conn, operation, requested_by=operator)
    gate.vote(conn, approval["id"], operator, True)
    work.release_claim(conn, old_claim, owner)
    new_claim, _, _ = work.claim_task(
        conn, task, owner, "feat/new", ["src/**"]
    )
    conn.commit()

    result = _cli(
        cfg,
        "claim",
        str(task),
        "--takeover",
        "--branch",
        "feat/taken",
        "--scope",
        "src/**",
        "--no-worktree",
        agent=successor,
    )
    assert result.returncode == 1
    assert "requires approval" in result.stderr
    holder = work.active_claim_for_task(conn, task)
    assert holder is not None and holder["id"] == new_claim
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"


def test_takeover_request_must_match_live_incumbent(conn, git_repo):
    repository = f"validate-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repository, git_repo)
    _agents(conn, "owner", "successor", "operator")
    task = work.create_task(conn, repository, "validate takeover")
    claim, _, _ = work.claim_task(conn, task, "owner", "feat/x", ["src/**"])
    work.release_claim(conn, claim, "owner")
    operation = {
        "type": "lease_takeover",
        "repository": repository,
        "task_id": task,
        "from_claim_id": claim,
        "from_agent": "owner",
        "to_agent": "successor",
    }

    with pytest.raises(gate.GateError, match="current live incumbent"):
        gate.request_approval(conn, operation, requested_by="operator")


def test_unregistered_pusher_is_rejected_on_unclaimed_branch(
    committed_conn, tmp_path, cfg
):
    _repo, _hub, clone, _a, _b = _setup(committed_conn, tmp_path)
    _commit(clone, "free.txt", branch="feat/free")

    result = _push(clone, "ghost", "feat/free", cfg=cfg)
    assert result.returncode != 0
    assert "not registered" in result.stderr
