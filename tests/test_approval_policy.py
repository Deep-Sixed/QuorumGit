"""Approver rosters, 2/3 + 1 quorum, and separation of duties."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from quorumgit import audit, gate, registry
from tests.test_gate import _commit, _push, _setup


def _names(*roles: str) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"{role}-{suffix}" for role in roles]


def _repo(conn, git_repo, *approvers: str, separate_duties: bool = False) -> str:
    name = f"policy-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, name, git_repo)
    for approver in approvers:
        registry.add_approver(conn, name, approver)
    if separate_duties:
        registry.set_separate_duties(conn, name, True)
    return name


def _agents(conn, *names: str) -> None:
    for name in names:
        registry.add_agent(conn, name)


def _op(repo: str) -> dict:
    return {"type": "protected_ref_update", "repository": repo, "n": uuid.uuid4().hex}


@pytest.mark.parametrize(
    ("members", "threshold"),
    [(1, 1), (2, 2), (3, 3), (4, 3), (5, 4), (6, 5), (7, 5), (9, 7)],
)
def test_quorum_threshold_is_two_thirds_plus_one(members, threshold):
    assert gate.quorum_threshold(members) == threshold


def test_roster_sets_quorum_and_excludes_outsiders(conn, git_repo):
    a1, a2, a3, a4, outsider = _names("a1", "a2", "a3", "a4", "outsider")
    _agents(conn, a1, a2, a3, a4, outsider)
    repo = _repo(conn, git_repo, a1, a2, a3, a4)

    approval = gate.request_approval(conn, _op(repo), requested_by=outsider)
    with pytest.raises(gate.GateError, match="not an approver"):
        gate.vote(conn, approval["id"], outsider, True)
    assert gate.vote(conn, approval["id"], a1, True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], a2, True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], a3, True)["status"] == "approved"


def test_request_threshold_can_raise_but_not_lower_quorum(conn, git_repo):
    a1, a2, a3 = _names("a1", "a2", "a3")
    _agents(conn, a1, a2, a3)
    repo = _repo(conn, git_repo, a1, a2)

    low = gate.request_approval(conn, _op(repo), requested_by=a1, threshold=1)
    assert gate.vote(conn, low["id"], a1, True)["status"] == "pending"
    assert gate.vote(conn, low["id"], a2, True)["status"] == "approved"

    registry.add_approver(conn, repo, a3)
    high = gate.request_approval(conn, _op(repo), requested_by=a1, threshold=3)
    gate.vote(conn, high["id"], a1, True)
    assert gate.vote(conn, high["id"], a2, True)["status"] == "pending"
    assert gate.vote(conn, high["id"], a3, True)["status"] == "approved"


def test_removed_approver_vote_stops_counting_at_use(conn, git_repo):
    a1, a2, a3, pusher = _names("a1", "a2", "a3", "pusher")
    _agents(conn, a1, a2, a3, pusher)
    repo = _repo(conn, git_repo, a1, a2)
    op = _op(repo)
    approval = gate.request_approval(conn, op, requested_by=pusher)
    gate.vote(conn, approval["id"], a1, True)
    assert gate.vote(conn, approval["id"], a2, True)["status"] == "approved"

    registry.remove_approver(conn, repo, a2)
    registry.add_approver(conn, repo, a3)
    with pytest.raises(gate.GateError, match="1 eligible yes vote"):
        gate.consume_approval(conn, approval["id"], op, agent=pusher)

    # The approval is still live: a current approver can restore quorum.
    gate.vote(conn, approval["id"], a3, True)
    gate.consume_approval(conn, approval["id"], op, agent=pusher)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"


def test_deny_revokes_an_unused_approval(conn, git_repo):
    a1, a2 = _names("a1", "a2")
    _agents(conn, a1, a2)
    repo = _repo(conn, git_repo)
    op = _op(repo)
    approval = gate.request_approval(conn, op, requested_by=a1)
    assert gate.vote(conn, approval["id"], a1, True)["status"] == "approved"
    assert gate.vote(conn, approval["id"], a2, False)["status"] == "denied"
    with pytest.raises(gate.GateError, match="not consumable"):
        gate.consume_approval(conn, approval["id"], op, agent=a1)


def test_separate_duties_blocks_requester_vote(conn, git_repo):
    requester, other = _names("requester", "other")
    _agents(conn, requester, other)
    repo = _repo(conn, git_repo, separate_duties=True)
    approval = gate.request_approval(conn, _op(repo), requested_by=requester)
    with pytest.raises(gate.GateError, match="separation of duties"):
        gate.vote(conn, approval["id"], requester, True)
    assert gate.vote(conn, approval["id"], other, True)["status"] == "approved"


def test_separate_duties_discounts_consumers_own_vote(conn, git_repo):
    requester, pusher, other = _names("requester", "pusher", "other")
    _agents(conn, requester, pusher, other)
    repo = _repo(conn, git_repo, separate_duties=True)
    op = _op(repo)
    approval = gate.request_approval(conn, op, requested_by=requester)
    assert gate.vote(conn, approval["id"], pusher, True)["status"] == "approved"

    with pytest.raises(gate.GateError, match="own vote"):
        gate.consume_approval(conn, approval["id"], op, agent=pusher)
    # Any agent whose own vote is not the deciding one may use it.
    gate.consume_approval(conn, approval["id"], op, agent=other)
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"


def test_open_policy_is_unchanged_without_roster(conn, git_repo):
    solo = _names("solo")[0]
    _agents(conn, solo)
    repo = _repo(conn, git_repo)
    op = _op(repo)
    approval = gate.request_approval(conn, op, requested_by=solo)
    assert gate.vote(conn, approval["id"], solo, True)["status"] == "approved"
    gate.consume_approval(conn, approval["id"], op, agent=solo)


def test_policy_changes_are_audited(conn, git_repo):
    member = _names("member")[0]
    _agents(conn, member)
    repo = _repo(conn, git_repo, member, separate_duties=True)
    repo_id = registry.get_repository(conn, repo)["id"]
    registry.remove_approver(conn, repo, member)
    kinds = [
        e["event_type"]
        for e in audit.events(conn, entity="repository", entity_id=repo_id)
    ]
    assert "repository.approver_added" in kinds
    assert "repository.approver_removed" in kinds
    assert "repository.policy_changed" in kinds


def test_roster_rejects_duplicates_and_unknown_members(conn, git_repo):
    member = _names("member")[0]
    _agents(conn, member)
    repo = _repo(conn, git_repo, member)
    with pytest.raises(registry.RegistryError, match="already an approver"):
        registry.add_approver(conn, repo, member)
    with pytest.raises(registry.RegistryError, match="not registered"):
        registry.add_approver(conn, repo, "nobody-" + uuid.uuid4().hex)
    registry.remove_approver(conn, repo, member)
    with pytest.raises(registry.RegistryError, match="not an approver"):
        registry.remove_approver(conn, repo, member)


def test_separate_duties_quorum_counts_only_eligible_approvers(conn, git_repo):
    """Excluding the requester and consumer never makes quorum impossible."""
    a, b, c, pusher = _names("a", "b", "c", "pusher")
    _agents(conn, a, b, c, pusher)
    repo = _repo(conn, git_repo, a, b, c, separate_duties=True)
    op = _op(repo)
    # b requests: a and c are eligible, so quorum is 2 of 2.
    approval = gate.request_approval(conn, op, requested_by=b)
    assert gate.vote(conn, approval["id"], a, True)["status"] == "pending"
    assert gate.vote(conn, approval["id"], c, True)["status"] == "approved"
    # a uses it: only c is eligible, c approved, so quorum (1 of 1) holds.
    gate.consume_approval(conn, approval["id"], op, agent=a)


def test_sole_approver_cannot_approve_own_push(conn, git_repo):
    solo = _names("solo")[0]
    _agents(conn, solo)
    repo = _repo(conn, git_repo, solo, separate_duties=True)
    op = _op(repo)
    approval = gate.request_approval(conn, op, requested_by=solo)
    with pytest.raises(gate.GateError, match="separation of duties"):
        gate.vote(conn, approval["id"], solo, True)


def test_hub_pusher_cannot_approve_own_push_under_separate_duties(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    registry.set_separate_duties(conn, repo_name, True)
    reviewer = _names("reviewer")[0]
    _agents(conn, reviewer)
    conn.commit()

    oldrev = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _commit(clone, "release.txt")
    newrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    op = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": oldrev,
        "newrev": newrev,
    }
    approval = gate.request_approval(conn, op, requested_by=b)
    assert gate.vote(conn, approval["id"], a, True)["status"] == "approved"
    conn.commit()

    own = _push(clone, a, "main", cfg=cfg)
    assert own.returncode != 0
    assert "own vote" in own.stderr

    gate.vote(conn, approval["id"], reviewer, True)
    conn.commit()
    reviewed = _push(clone, a, "main", cfg=cfg)
    assert reviewed.returncode == 0, reviewed.stderr
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"


def test_cli_manages_roster_and_policy(committed_conn, git_repo, cfg):
    member, other = _names("member", "other")
    _agents(committed_conn, member, other)
    repo = _repo(committed_conn, git_repo)
    committed_conn.commit()

    def cli(*args):
        env = {**os.environ, "QUORUMGIT_DATA_DIR": str(cfg.data_dir)}
        return subprocess.run(
            [sys.executable, "-m", "quorumgit", *args],
            capture_output=True, text=True, env=env, check=False,
        )

    assert cli("repo", "approver", "add", repo, member).returncode == 0
    assert cli("repo", "approver", "add", repo, other).returncode == 0
    listed = cli("repo", "approver", "list", repo)
    assert listed.stdout.split() == sorted([member, other])

    policy = cli("repo", "policy", repo, "--separate-duties")
    assert policy.returncode == 0, policy.stderr
    assert "quorum: 2 of 2" in policy.stdout
    assert "separate duties: on" in policy.stdout

    assert cli("repo", "approver", "remove", repo, other).returncode == 0
    policy = cli("repo", "policy", repo, "--no-separate-duties")
    assert "quorum: 1 of 1" in policy.stdout
    assert "separate duties: off" in policy.stdout

    duplicate = cli("repo", "approver", "add", repo, member)
    assert duplicate.returncode == 1
    assert "already an approver" in duplicate.stderr
