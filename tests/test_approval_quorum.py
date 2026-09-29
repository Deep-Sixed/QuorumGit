"""Quorum mode: 2/3 + 1 of the agents eligible to approve an operation.

Roles are global, so these tests use an isolated store where the number of
agents holding each role is known exactly.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from quorumgit import gate, registry, store
from quorumgit.config import Config
from tests.conftest import register_repo


@pytest.fixture()
def quorum_store(tmp_path):
    local = Config(data_dir=tmp_path / "quorum-data", agent=None)
    store.migrate(local)
    conn = store.connect(local)
    try:
        yield local, conn
    finally:
        conn.close()
        store.destroy(local)


def _operators(conn, *names: str) -> None:
    """Register operators; the first is bootstrapped, the rest added by it."""
    registry.add_agent(conn, names[0])
    registry.set_agent_role(conn, names[0], "operator", actor=None)
    for name in names[1:]:
        registry.add_agent(conn, name, role="operator", actor=names[0])


def _op(repo: str, n: int = 0) -> dict:
    return {"type": "protected_ref_update", "repository": repo, "n": n}


@pytest.mark.parametrize(
    ("eligible", "required"),
    [(0, 1), (1, 1), (2, 2), (3, 3), (4, 3), (5, 4), (6, 5), (7, 5), (9, 7)],
)
def test_quorum_threshold_is_two_thirds_plus_one(eligible, required):
    assert gate.quorum_threshold(eligible) == required


def test_quorum_counts_eligible_approvers(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a", "b", "c", "d")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q1")
    registry.set_approval_policy(conn, repo, actor="a", quorum=True)

    # Four operators may vote on a worker's request: 3 of 4 are needed.
    approval = gate.request_approval(conn, _op(repo), "worker")
    assert approval["threshold"] == 3
    assert gate.vote(conn, approval["id"], "a", True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], "b", True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], "c", True)["status"] == "approved"
    gate.consume_approval(conn, approval["id"], _op(repo), agent="worker")


def test_requester_exclusion_shrinks_the_eligible_set(quorum_store, tmp_path):
    """An operator's own request is decided by the others: 2 of 2, not 3 of 3."""
    _local, conn = quorum_store
    _operators(conn, "a", "b", "c")
    registry.add_agent(conn, "pusher")
    repo = register_repo(conn, tmp_path / "q2")
    registry.set_approval_policy(conn, repo, actor="a", quorum=True)

    approval = gate.request_approval(conn, _op(repo), "a")
    assert approval["threshold"] == 2
    gate.vote(conn, approval["id"], "b", True)
    assert gate.vote(conn, approval["id"], "c", True)["status"] == "approved"
    gate.consume_approval(conn, approval["id"], _op(repo), agent="pusher")


def test_status_and_usability_use_the_same_requirement(quorum_store, tmp_path):
    """A non-voting operator carrying out the operation does not lower quorum.

    Five operators are eligible on a worker's request, so 4 are needed. With 3
    yes votes the approval is pending for everyone, including an operator who
    did not vote; it becomes usable exactly when it becomes approved.
    """
    _local, conn = quorum_store
    _operators(conn, "a", "b", "c", "d", "e")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q-consumer")
    registry.set_approval_policy(conn, repo, actor="a", quorum=True)

    op = _op(repo)
    approval = gate.request_approval(conn, op, "worker")
    assert approval["threshold"] == 4
    for voter in ("a", "b", "c"):
        approval = gate.vote(conn, approval["id"], voter, True)
    assert approval["status"] == "pending"
    assert gate.approved_instance(conn, op, consumer="d") is None
    with pytest.raises(gate.GateError, match="not consumable"):
        gate.consume_approval(conn, approval["id"], op, agent="d")

    assert gate.vote(conn, approval["id"], "e", True)["status"] == "approved"
    assert gate.approved_instance(conn, op, consumer="d") is not None
    gate.consume_approval(conn, approval["id"], op, agent="d")


def test_fixed_threshold_is_a_floor(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a", "b", "c", "d")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q3")
    registry.set_approval_policy(conn, repo, actor="a", quorum=True, threshold=4)
    approval = gate.request_approval(conn, _op(repo), "worker")
    assert approval["threshold"] == 4


def test_new_approver_raises_quorum_for_unused_approvals(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a", "b")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q4")
    registry.set_approval_policy(conn, repo, actor="a", quorum=True)

    op = _op(repo)
    approval = gate.request_approval(conn, op, "worker")
    gate.vote(conn, approval["id"], "a", True)
    assert gate.vote(conn, approval["id"], "b", True)["status"] == "approved"

    # Two more operators: 2 of 4 no longer meets 2/3 + 1 (3).
    registry.add_agent(conn, "c", role="operator", actor="a")
    registry.add_agent(conn, "d", role="operator", actor="a")
    assert not gate.is_approved(conn, op)
    with pytest.raises(gate.GateError, match=r"now requires 3 \(2/3 \+ 1"):
        gate.consume_approval(conn, approval["id"], op, agent="worker")


def test_quorum_respects_approving_roles(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a", "b")
    registry.add_agent(conn, "r1", role="reviewer", actor="a")
    registry.add_agent(conn, "r2", role="reviewer", actor="a")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q5")
    registry.set_approval_policy(
        conn, repo, actor="a", quorum=True, roles=["operator", "reviewer"]
    )
    # Operators and reviewers both count: 4 eligible, so 3 are needed.
    approval = gate.request_approval(conn, _op(repo), "worker")
    assert approval["threshold"] == 3

    registry.set_approval_policy(conn, repo, actor="a", roles=["reviewer"])
    other = gate.request_approval(conn, _op(repo, 1), "worker")
    assert other["threshold"] == 2


def test_quorum_off_keeps_fixed_threshold(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a", "b", "c", "d")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q6")
    approval = gate.request_approval(conn, _op(repo), "worker")
    assert approval["threshold"] == 1
    assert gate.vote(conn, approval["id"], "a", True)["status"] == "approved"


def test_quorum_change_is_audited_and_operator_only(quorum_store, tmp_path):
    _local, conn = quorum_store
    _operators(conn, "a")
    registry.add_agent(conn, "worker")
    repo = register_repo(conn, tmp_path / "q7")
    conn.commit()
    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_approval_policy(conn, repo, actor="worker", quorum=True)
    conn.rollback()
    registry.set_approval_policy(conn, repo, actor="a", quorum=True)
    row = conn.execute(
        "SELECT detail FROM audit_events WHERE event_type = "
        "'repository.policy_changed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = store.json_loads(row[0], {})
    assert detail["before"]["quorum"] is False
    assert detail["after"]["quorum"] is True


def test_cli_quorum_policy(quorum_store, tmp_path):
    local, conn = quorum_store
    _operators(conn, "a", "b", "c")
    repo = register_repo(conn, tmp_path / "q8")
    conn.commit()

    def cli(*args):
        env = {**os.environ, "QUORUMGIT_DATA_DIR": str(local.data_dir)}
        env.pop("QUORUMGIT_AGENT", None)
        return subprocess.run(
            [sys.executable, "-m", "quorumgit", *args],
            capture_output=True, text=True, env=env, check=False,
        )

    shown = cli("repo", "policy", repo, "--quorum", "--agent", "a")
    assert shown.returncode == 0, shown.stderr
    assert "2/3 + 1" in shown.stdout
    assert "3 agent(s) hold an approving role" in shown.stdout

    off = cli("repo", "policy", repo, "--no-quorum", "--agent", "a")
    assert "approval threshold: 1" in off.stdout
