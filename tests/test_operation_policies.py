"""Per-operation approval policy: overrides of the repository default."""

from __future__ import annotations

import sqlite3

import pytest

from quorumgit import audit, cli, gate, registry, store
from quorumgit.config import Config
from tests.conftest import OPERATOR, ensure_agent, make_git_repo


def _op(repo: str, kind: str, n: int = 1) -> dict:
    return {
        "type": kind,
        "repository": repo,
        "refname": "refs/heads/feat/x",
        "oldrev": "a" * 40,
        "newrev": f"{n:040d}",
    }


@pytest.fixture()
def people(conn):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "op-2", "operator")
    ensure_agent(conn, "rev-1", "reviewer")
    ensure_agent(conn, "worker-1")
    return conn


def test_override_threshold_applies_only_to_its_operation(people, approval_repo):
    conn = people
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, operation_type="force_update", threshold=2
    )

    forced = gate.request_approval(
        conn, _op(approval_repo, "force_update"), requested_by="worker-1"
    )
    assert forced["threshold"] == 2
    assert gate.vote(conn, forced["id"], OPERATOR, True)["status"] == "pending"
    assert gate.vote(conn, forced["id"], "op-2", True)["status"] == "approved"

    protected = gate.request_approval(
        conn, _op(approval_repo, "protected_ref_update"), requested_by="worker-1"
    )
    assert protected["threshold"] == 1
    assert gate.vote(conn, protected["id"], OPERATOR, True)["status"] == "approved"


def test_override_roles_replace_the_default_roles(people, approval_repo):
    conn = people
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR,
        operation_type="out_of_scope_push", roles=["reviewer"],
    )
    approval = gate.request_approval(
        conn, _op(approval_repo, "out_of_scope_push"), requested_by="worker-1"
    )
    with pytest.raises(gate.GateError, match="accepts votes only from: reviewer"):
        gate.vote(conn, approval["id"], OPERATOR, True)
    # The database enforces the same effective policy for direct writers.
    operator_id = registry.get_agent(conn, OPERATOR)["id"]
    with pytest.raises(sqlite3.IntegrityError, match="not eligible"):
        conn.execute(
            "INSERT INTO votes (approval_id, voter, vote, voter_agent_id) "
            "VALUES (?, ?, 1, ?)",
            (approval["id"], OPERATOR, operator_id),
        )
    assert gate.vote(conn, approval["id"], "rev-1", True)["status"] == "approved"

    # Other operation types keep the repository's default roles.
    other = gate.request_approval(
        conn, _op(approval_repo, "ref_delete"), requested_by="worker-1"
    )
    with pytest.raises(gate.GateError, match="accepts votes only from: operator"):
        gate.vote(conn, other["id"], "rev-1", True)


def test_override_can_let_the_requester_vote(people, approval_repo):
    conn = people
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR,
        operation_type="protected_path_update", requester_may_vote=True,
    )
    approval = gate.request_approval(
        conn, _op(approval_repo, "protected_path_update"), requested_by=OPERATOR
    )
    assert gate.vote(conn, approval["id"], OPERATOR, True)["status"] == "approved"
    # Still never the one who carries it out.
    operation = _op(approval_repo, "protected_path_update")
    assert gate.approved_instance(conn, operation, consumer=OPERATOR) is None
    assert gate.approved_instance(conn, operation, consumer="worker-1") is not None


def test_raising_an_override_invalidates_existing_approvals(people, approval_repo):
    conn = people
    operation = _op(approval_repo, "force_update")
    approval = gate.request_approval(conn, operation, requested_by="worker-1")
    gate.vote(conn, approval["id"], OPERATOR, True)
    assert gate.approved_instance(conn, operation, consumer="worker-1") is not None

    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, operation_type="force_update", threshold=2
    )
    assert gate.approved_instance(conn, operation, consumer="worker-1") is None
    with pytest.raises(gate.GateError, match="now requires 2"):
        gate.consume_approval(conn, approval["id"], operation, agent="worker-1")

    # The same live instance is reopened and can collect the missing vote.
    assert gate.request_approval(conn, operation, requested_by="worker-1")["id"] == (
        approval["id"]
    )
    topped_up = gate.vote(conn, approval["id"], "op-2", True)
    assert topped_up["status"] == "approved"
    assert gate.approved_instance(conn, operation, consumer="worker-1") is not None
    events = [e["event_type"] for e in audit.events(conn, entity="approval",
                                                   entity_id=approval["id"])]
    assert "approval.reopened" in events


def test_a_stale_approval_can_also_be_denied(people, approval_repo):
    conn = people
    operation = _op(approval_repo, "ref_delete")
    approval = gate.request_approval(conn, operation, requested_by="worker-1")
    gate.vote(conn, approval["id"], OPERATOR, True)
    # Still valid: no reopening, so it stays terminal for further votes.
    with pytest.raises(gate.GateError, match="already approved"):
        gate.vote(conn, approval["id"], "op-2", True)

    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, operation_type="ref_delete", threshold=2
    )
    assert gate.vote(conn, approval["id"], "op-2", False)["status"] == "denied"


def test_contract_requires_the_effective_policy_view(tmp_path):
    local = Config(data_dir=tmp_path / "contract", agent=None)
    store.migrate(local)
    conn = store.connect(local)
    try:
        conn.execute("DROP VIEW approval_effective_policy")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(store.ContractViolation, match="approval_effective_policy"):
        store.verify_contract(local)
    store.destroy(local)


def test_unset_override_fields_inherit_and_inherit_removes(people, approval_repo):
    conn = people
    repo_id = registry.get_repository(conn, approval_repo)["id"]
    registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, operation_type="ref_delete", threshold=3
    )
    # The default changing later still reaches fields the override left unset.
    registry.set_approval_policy(conn, approval_repo, actor=OPERATOR, roles=["reviewer"])
    assert registry.approval_policy(conn, repo_id, "ref_delete") == {
        "threshold": 3,
        "requester_may_vote": False,
        "roles": ["reviewer"],
    }

    effective = registry.set_approval_policy(
        conn, approval_repo, actor=OPERATOR, operation_type="ref_delete", inherit=True
    )
    assert effective == registry.approval_policy(conn, repo_id)
    assert registry.operation_policy_overrides(conn, repo_id) == {}
    changes = [
        e for e in audit.events(conn, entity="repository", entity_id=repo_id)
        if e["event_type"] == "repository.policy_changed"
    ]
    assert len(changes) == 3


def test_override_changes_are_validated_and_operator_only(people, approval_repo):
    conn = people
    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_approval_policy(
            conn, approval_repo, actor="worker-1",
            operation_type="force_update", threshold=2,
        )
    with pytest.raises(registry.RegistryError, match="Unknown operation type"):
        registry.set_approval_policy(
            conn, approval_repo, actor=OPERATOR, operation_type="test_op", threshold=2
        )
    with pytest.raises(registry.RegistryError, match="cannot be combined"):
        registry.set_approval_policy(
            conn, approval_repo, actor=OPERATOR,
            operation_type="force_update", threshold=2, inherit=True,
        )
    with pytest.raises(registry.RegistryError, match="only to an --operation"):
        registry.set_approval_policy(conn, approval_repo, actor=OPERATOR, inherit=True)


def test_cli_shows_and_sets_overrides(
    committed_conn, tmp_path, cfg, monkeypatch, capsys
):
    conn = committed_conn
    ensure_agent(conn, OPERATOR, "operator")
    name = "policy-cli-" + tmp_path.name[-8:]
    registry.add_repository(conn, name, make_git_repo(tmp_path / "repo"))
    conn.commit()
    monkeypatch.setenv("QUORUMGIT_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("QUORUMGIT_AGENT", OPERATOR)

    assert cli.main([
        "repo", "policy", name, "--operation", "force_update",
        "--threshold", "2", "--role", "operator", "--role", "reviewer",
    ]) == 0
    out = capsys.readouterr().out
    assert "force_update (override): threshold 2; roles operator, reviewer" in out

    assert cli.main(["repo", "policy", name]) == 0
    out = capsys.readouterr().out
    assert "per-operation overrides:" in out
    assert "  force_update: threshold 2" in out

    assert cli.main(["repo", "policy", name, "--operation", "force_update",
                     "--inherit"]) == 0
    out = capsys.readouterr().out
    assert "force_update (inherits default): threshold 1" in out
