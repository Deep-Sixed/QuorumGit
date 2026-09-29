"""Approval authority: who may authorize is owned by the repository.

A worker cannot authorize its own operation: the requester does not choose the
quorum, only roles the repository names may vote, the requester may not vote
unless the policy allows it, and an agent that approved an operation can never
be the one that carries it out. Authority is re-derived at use time, so a
tightened policy or a demoted approver invalidates an earlier approval.
"""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, registry, store, work
from quorumgit.config import Config
from quorumgit.registry import RegistryError
from tests.conftest import (
    OPERATOR,
    approve,
    ensure_agent,
    make_git_repo,
    register_repo,
)
from tests.test_authorization_binding import _apply_001
from tests.test_cli_hub import _cli
from tests.test_gate import _commit, _push, _setup


def _op(repository: str, **extra) -> dict:
    return {"type": "protected_ref_update", "repository": repository,
            "nonce": uuid.uuid4().hex, **extra}


# ------------------------------------------------------------- voting rules


def test_worker_cannot_approve_its_own_request(conn, approval_repo):
    ensure_agent(conn, "self-worker")
    approval = gate.request_approval(conn, _op(approval_repo), "self-worker")

    with pytest.raises(gate.GateError, match="is a worker"):
        gate.vote(conn, approval["id"], "self-worker", True)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "pending"


def test_operator_requester_cannot_vote_by_default(conn, approval_repo):
    ensure_agent(conn, "requesting-op", "operator")
    approval = gate.request_approval(conn, _op(approval_repo), "requesting-op")

    with pytest.raises(gate.GateError, match="requested this approval"):
        gate.vote(conn, approval["id"], "requesting-op", True)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "pending"


def test_policy_can_allow_requester_to_vote(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "requesting-op", "operator")
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, requester_may_vote=True
    )
    approval = gate.request_approval(conn, _op(approval_repo), "requesting-op")
    result = gate.vote(conn, approval["id"], "requesting-op", True)
    assert result["status"] == "approved"


def test_non_approving_role_cannot_deny_either(conn, approval_repo):
    ensure_agent(conn, "req")
    ensure_agent(conn, "a-reviewer", "reviewer")
    approval = gate.request_approval(conn, _op(approval_repo), "req")
    with pytest.raises(gate.GateError, match="accepts votes only from: operator"):
        gate.vote(conn, approval["id"], "a-reviewer", False)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "pending"


def test_policy_roles_decide_who_votes(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "req")
    ensure_agent(conn, "a-reviewer", "reviewer")
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, roles=["reviewer"]
    )
    approval = gate.request_approval(conn, _op(approval_repo), "req")
    with pytest.raises(gate.GateError, match="accepts votes only from: reviewer"):
        gate.vote(conn, approval["id"], OPERATOR, True)
    assert gate.vote(conn, approval["id"], "a-reviewer", True)["status"] == "approved"


def test_engine_refuses_ineligible_vote_rows(conn, approval_repo):
    """The vote rule holds even for a writer that bypasses the gate module."""
    ensure_agent(conn, "raw-worker")
    approval = gate.request_approval(conn, _op(approval_repo), "raw-worker")
    worker_id = registry.get_agent(conn, "raw-worker")["id"]
    with pytest.raises(sqlite3.IntegrityError, match="not eligible"):
        conn.execute(
            "INSERT INTO votes (approval_id, voter, vote, voter_agent_id) "
            "VALUES (?, 'raw-worker', 1, ?)",
            (approval["id"], worker_id),
        )


def test_approval_must_name_a_registered_repository(conn):
    ensure_agent(conn, "req")
    with pytest.raises(gate.GateError, match="registered repository"):
        gate.request_approval(conn, _op("no-such-repo"), "req")


# -------------------------------------------------------------- threshold


def test_threshold_comes_from_repository_policy(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "second-op", "operator")
    ensure_agent(conn, "req")
    registry.set_approval_policy(conn, approval_repo, actor=OPERATOR, threshold=2)
    approval = gate.request_approval(conn, _op(approval_repo), "req")
    assert approval["threshold"] == 2
    assert gate.vote(conn, approval["id"], OPERATOR, True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], "second-op", True)["status"] == "approved"


def test_cli_requester_cannot_choose_threshold(committed_conn, cfg, tmp_path):
    repo = register_repo(committed_conn, tmp_path / "cli-threshold")
    ensure_agent(committed_conn, "cli-req")
    committed_conn.commit()
    result = _cli(
        cfg, "approve", "request", f'{{"type":"x","repository":"{repo}"}}',
        "--threshold", "1", agent="cli-req",
    )
    assert result.returncode == 2
    assert "--threshold" in result.stderr


def test_tightened_policy_invalidates_existing_approval(conn, approval_repo):
    op = _op(approval_repo)
    approval = approve(conn, op, requested_by="req")
    ensure_agent(conn, "pusher")
    assert gate.is_approved(conn, op)

    registry.set_approval_policy(conn, approval_repo, actor=OPERATOR, threshold=2)
    assert not gate.is_approved(conn, op)
    with pytest.raises(gate.GateError, match="now requires 2"):
        gate.consume_approval(conn, approval["id"], op, agent="pusher")
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"


def test_demoted_approver_no_longer_counts(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "soon-demoted", "operator")
    ensure_agent(conn, "pusher")
    op = _op(approval_repo)
    approval = approve(conn, op, requested_by="req", voters=("soon-demoted",))
    registry.set_agent_role(conn, "soon-demoted", "worker", actor=OPERATOR)
    with pytest.raises(gate.GateError, match="0 eligible"):
        gate.consume_approval(conn, approval["id"], op, agent="pusher")


# ------------------------------------------------------------ consumption


def test_approver_cannot_consume_its_own_approval(conn, approval_repo):
    ensure_agent(conn, "req")
    op = _op(approval_repo)
    approval = approve(conn, op, requested_by="req")
    assert gate.approved_instance(conn, op, consumer=OPERATOR) is None
    with pytest.raises(gate.GateError, match="may not also carry it out"):
        gate.consume_approval(conn, approval["id"], op, agent=OPERATOR)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"


def test_engine_refuses_consumption_by_approver(conn, approval_repo):
    op = _op(approval_repo)
    approval = approve(conn, op, requested_by="req")
    operator_id = registry.get_agent(conn, OPERATOR)["id"]
    with pytest.raises(sqlite3.IntegrityError, match="cannot consume its own approval"):
        conn.execute(
            "UPDATE approvals SET status = 'consumed', consumed_at = unixepoch(), "
            "consumed_by_agent_id = ? WHERE id = ?",
            (operator_id, approval["id"]),
        )


def test_takeover_beneficiary_cannot_vote(conn, git_repo):
    repo = f"beneficiary-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo, git_repo)
    ensure_agent(conn, "holder")
    ensure_agent(conn, "ambitious-op", "operator")
    task = work.create_task(conn, repo, "held")
    claim, _, _ = work.claim_task(conn, task, "holder", "feat/held", ["src/**"])
    operation = {
        "type": "lease_takeover",
        "repository": repo,
        "task_id": task,
        "from_claim_id": claim,
        "from_agent": "holder",
        "to_agent": "ambitious-op",
    }
    ensure_agent(conn, "req")
    approval = gate.request_approval(conn, operation, "req")
    with pytest.raises(gate.GateError, match="would receive this takeover"):
        gate.vote(conn, approval["id"], "ambitious-op", True)


def test_pusher_that_approved_is_rejected_by_hook(committed_conn, tmp_path, cfg):
    repo_name, hub, clone, a, _b = _setup(committed_conn, tmp_path)
    approver = f"approver-{uuid.uuid4().hex[:8]}"
    ensure_agent(committed_conn, approver, "operator")
    _commit(clone, "approved-by-pusher.txt")
    op = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": subprocess.run(
            ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip(),
        "newrev": subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip(),
    }
    approval = approve(committed_conn, op, requested_by=a, voters=(approver,))
    committed_conn.commit()

    refused = _push(clone, approver, "main", cfg=cfg)
    assert refused.returncode != 0
    assert "may not also carry it out" in refused.stderr
    assert gate.get_approval_by_id(committed_conn, approval["id"])["status"] == (
        "approved"
    )

    accepted = _push(clone, a, "main", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr


# ------------------------------------------------- operator administration


@pytest.fixture()
def fresh_store(tmp_path):
    local = Config(data_dir=tmp_path / "authority-data", agent=None)
    store.migrate(local)
    conn = store.connect(local)
    try:
        yield local, conn
    finally:
        conn.close()
        store.destroy(local)


def test_first_operator_is_bootstrapped_then_roles_are_operator_only(
    fresh_store, tmp_path
):
    _local, conn = fresh_store
    registry.add_agent(conn, "alice")
    registry.add_agent(conn, "mallory")
    repo = register_repo(conn, tmp_path / "bootstrap")

    # No operator exists yet, so the first designation needs no authority.
    registry.set_agent_role(conn, "alice", "operator", actor=None)
    conn.commit()

    with pytest.raises(RegistryError, match="requires an operator"):
        registry.set_agent_role(conn, "mallory", "operator", actor="mallory")
    conn.rollback()
    with pytest.raises(RegistryError, match="requires an operator"):
        registry.add_agent(conn, "eve", role="operator", actor=None)
    conn.rollback()
    with pytest.raises(RegistryError, match="requires an operator"):
        registry.set_approval_policy(conn, repo, actor="mallory", threshold=1)
    conn.rollback()

    registry.set_agent_role(conn, "mallory", "reviewer", actor="alice")
    registry.set_approval_policy(conn, repo, actor="alice", roles=["operator", "reviewer"])
    conn.commit()
    assert registry.get_agent(conn, "mallory")["role"] == "reviewer"
    policy = registry.approval_policy(conn, registry.get_repository(conn, repo)["id"])
    assert policy["roles"] == ["operator", "reviewer"]

    events = {
        row[0]
        for row in conn.execute("SELECT event_type FROM audit_events").fetchall()
    }
    assert {"agent.role_changed", "repository.policy_changed"} <= events


def test_last_operator_cannot_be_demoted(fresh_store):
    _local, conn = fresh_store
    registry.add_agent(conn, "solo")
    registry.set_agent_role(conn, "solo", "operator", actor=None)
    with pytest.raises(RegistryError, match="last operator"):
        registry.set_agent_role(conn, "solo", "worker", actor="solo")


def test_cli_agent_roles_and_policy(fresh_store, tmp_path):
    local, conn = fresh_store
    repo = register_repo(conn, tmp_path / "cli-policy")
    conn.commit()

    assert _cli(local, "agent", "add", "boss", "--role", "operator").returncode == 0
    refused = _cli(local, "agent", "add", "sneaky", "--role", "operator", agent="nobody")
    assert refused.returncode == 1
    assert _cli(local, "agent", "add", "worker-1").returncode == 0
    promoted = _cli(local, "agent", "role", "worker-1", "reviewer", agent="boss")
    assert promoted.returncode == 0, promoted.stderr

    listed = _cli(local, "agent", "list")
    assert "boss\toperator" in listed.stdout
    assert "worker-1\treviewer" in listed.stdout

    changed = _cli(
        local, "repo", "policy", repo, "--threshold", "2",
        "--role", "operator", "--role", "reviewer", "--requester-may-vote",
        agent="boss",
    )
    assert changed.returncode == 0, changed.stderr
    shown = _cli(local, "repo", "policy", repo)
    assert "approval threshold: 2" in shown.stdout
    assert "approving roles: operator, reviewer" in shown.stdout
    assert "requester may vote: yes" in shown.stdout


# ---------------------------------------------------------------- upgrade


def test_upgrade_leaves_no_agent_with_authority(tmp_path):
    """Pre-policy stores upgrade with every agent a worker, fail closed."""
    local = Config(data_dir=tmp_path / "upgrade", agent=None)
    conn = _apply_001(local)
    migration = Path(store.__file__).parent / "migrations" / "002_approval_identities.sql"
    for statement in store._migration_statements(migration.read_text(encoding="utf-8")):
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations (version) VALUES ('002_approval_identities.sql')"
    )
    repo_path = make_git_repo(tmp_path / "legacy-repo")
    conn.execute(
        "INSERT INTO repositories (name, path) VALUES ('legacy', ?)", (str(repo_path),)
    )
    agent_id = conn.execute(
        "INSERT INTO agents (name) VALUES ('self-approver') RETURNING id"
    ).fetchone()[0]
    operation = {"type": "protected_ref_update", "repository": "legacy"}
    approval_id = conn.execute(
        "INSERT INTO approvals (operation_hash, operation, status, "
        "requested_by_agent_id, decided_at) "
        "VALUES (?, ?, 'approved', ?, unixepoch()) RETURNING id",
        (gate.operation_hash(operation), store.json_dumps(operation), agent_id),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO votes (approval_id, voter, vote, voter_agent_id) "
        "VALUES (?, 'self-approver', 1, ?)",
        (approval_id, agent_id),
    )
    conn.commit()
    conn.close()

    assert store.migrate(local) == [
        "003_ref_updates.sql",
        "004_approval_authority.sql",
    ]
    upgraded = store.connect(local)
    try:
        assert registry.get_agent(upgraded, "self-approver")["role"] == "worker"
        repo = registry.get_repository(upgraded, "legacy")
        assert registry.approval_policy(upgraded, repo["id"]) == {
            "threshold": 1,
            "requester_may_vote": False,
            "roles": ["operator"],
        }
        backfilled = gate.get_approval_by_id(upgraded, approval_id)
        assert backfilled["repository_id"] == repo["id"]
        assert not gate.is_approved(upgraded, operation)
        upgraded.execute("INSERT INTO agents (name) VALUES ('other')")
        with pytest.raises(gate.GateError, match="0 eligible"):
            gate.consume_approval(upgraded, approval_id, operation, agent="other")
    finally:
        upgraded.rollback()
        upgraded.close()
        store.destroy(local)


def test_contract_requires_governance_triggers(fresh_store):
    local, conn = fresh_store
    conn.execute("DROP TRIGGER votes_require_eligible_voter")
    conn.commit()
    with pytest.raises(store.ContractViolation, match="votes_require_eligible_voter"):
        store.verify_contract(local)
