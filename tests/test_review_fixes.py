"""Regression coverage for the complete-review findings.

1. Voting is atomic: status always reflects all committed votes.
2. An approval authorizes a protected operation; it never bypasses claim
   ownership or an open-handoff freeze.
3. A refused takeover leaves the holder, the approval, and the task intact.
4. Handoff decline is addressee-only; cancel is creator-only.
5. Continuation commits must exist (and be reachable when the branch exists).
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid

import pytest

from quorumgit import gate, handoff, registry, store, trees, work
from quorumgit.registry import RegistryError
from tests.conftest import make_git_repo
from tests.test_cli_hub import _cli
from tests.test_gate import _commit, _push, _setup
from tests.test_handoff import _handoff_record, _head


# ------------------------------------------------------------ finding 1


def _pending_approval(conn, threshold=1):
    op = {
        "type": "protected_ref_update",
        "repository": "vote-race",
        "n": uuid.uuid4().hex,
    }
    gate.request_approval(conn, op, requested_by="op", threshold=threshold)
    return op


def _race_votes(initialized_store, op, first_vote, second_vote):
    """First voter holds the writer reservation while the second votes."""
    conn1 = store.connect(initialized_store)
    conn2 = store.connect(initialized_store)
    op_hash = gate.operation_hash(op)
    second_error: list[Exception] = []
    started = threading.Event()

    def racer():
        started.set()
        try:
            gate.vote(conn2, op_hash, "voter-2", second_vote)
            conn2.commit()
        except gate.GateError as exc:
            second_error.append(exc)
            conn2.rollback()

    try:
        gate.vote(conn1, op_hash, "voter-1", first_vote)
        thread = threading.Thread(target=racer)
        thread.start()
        started.wait(timeout=10)
        time.sleep(0.5)
        conn1.commit()
        thread.join(timeout=30)
        assert not thread.is_alive(), "second voter never returned"
        check = store.connect(initialized_store)
        try:
            return gate.get_approval(check, op_hash), second_error
        finally:
            check.close()
    finally:
        conn1.close()
        conn2.close()


def test_concurrent_no_and_yes_denial_stands(initialized_store):
    """A concurrent yes vote cannot override a denial."""
    setup = store.connect(initialized_store)
    op = _pending_approval(setup, threshold=1)
    setup.commit()
    setup.close()
    approval, second_error = _race_votes(
        initialized_store, op, first_vote=False, second_vote=True
    )
    assert approval["status"] == "denied"
    assert second_error, "yes vote on a denied approval must raise"


def test_concurrent_yes_votes_satisfy_threshold(initialized_store):
    """Two concurrent yes votes on a threshold-2 approval both count."""
    setup = store.connect(initialized_store)
    op = _pending_approval(setup, threshold=2)
    setup.commit()
    setup.close()
    approval, second_error = _race_votes(
        initialized_store, op, first_vote=True, second_vote=True
    )
    assert not second_error
    assert approval["status"] == "approved"


def test_denial_is_terminal(conn):
    op = _pending_approval(conn)
    op_hash = gate.operation_hash(op)
    gate.vote(conn, op_hash, "voter-1", False)
    assert gate.get_approval(conn, op_hash)["status"] == "denied"
    with pytest.raises(gate.GateError, match="already denied"):
        gate.vote(conn, op_hash, "voter-2", True)


def test_duplicate_voter_counts_once(conn):
    op = _pending_approval(conn, threshold=2)
    op_hash = gate.operation_hash(op)
    gate.vote(conn, op_hash, "voter-1", True)
    result = gate.vote(conn, op_hash, "voter-1", True)
    assert result["status"] == "pending"


# ------------------------------------------------------------ finding 2


def _approve_update(conn, repo_name, hub, clone, refname="refs/heads/main"):
    """Approve the exact pending clone→hub update of a ref."""
    oldrev = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", refname],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    newrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    op = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
    }
    gate.request_approval(conn, op, requested_by="operator")
    gate.vote(conn, gate.operation_hash(op), "operator", True)
    conn.commit()
    return op


def test_approval_does_not_bypass_claim_ownership(committed_conn, tmp_path, cfg):
    """A stranger with a valid approval still cannot push a claimed branch."""
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "protected claimed work")
    work.claim_task(conn, task, a, branch="main", scope_globs=["src/**"])
    conn.commit()

    _commit(clone, "src/protected.py")
    _approve_update(conn, repo_name, hub, clone)

    stranger = _push(clone, b, "main", cfg=cfg)
    assert stranger.returncode != 0
    assert "claimed by" in stranger.stderr

    owner = _push(clone, a, "main", cfg=cfg)
    assert owner.returncode == 0, owner.stderr


def test_approval_does_not_bypass_handoff_freeze(committed_conn, tmp_path, cfg):
    """An approved protected update cannot mutate a frozen branch."""
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "frozen protected work")
    claim_a, _, _ = work.claim_task(
        conn, task, a, branch="main", scope_globs=["src/**"]
    )
    handoff.create_handoff(conn, claim_a, a, _handoff_record(hub), to_agent=b)
    conn.commit()

    _commit(clone, "src/frozen.py")
    _approve_update(conn, repo_name, hub, clone)

    for pusher in (a, b):
        result = _push(clone, pusher, "main", cfg=cfg)
        assert result.returncode != 0
        assert "frozen pending handoff" in result.stderr


# ------------------------------------------------------------ finding 3


def test_refused_takeover_preserves_holder_and_approval(committed_conn, tmp_path, cfg):
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]
    repo_path = make_git_repo(tmp_path / "repo")
    repo_name = f"takeover-{suffix}"
    registry.add_repository(conn, repo_name, repo_path)
    a, b, c = f"a-{suffix}", f"b-{suffix}", f"c-{suffix}"
    for name in (a, b, c):
        registry.add_agent(conn, name)
    task1 = work.create_task(conn, repo_name, "held work")
    work.claim_task(conn, task1, a, branch="feat/one", scope_globs=["src/**"])
    task2 = work.create_task(conn, repo_name, "other work")
    work.claim_task(conn, task2, b, branch="feat/two", scope_globs=["docs/**"])

    operation = {
        "type": "lease_takeover",
        "repository": repo_name,
        "task_id": task1,
        "from_agent": a,
        "to_agent": c,
    }
    gate.request_approval(conn, operation, requested_by="operator")
    gate.vote(conn, gate.operation_hash(operation), "operator", True)
    conn.commit()

    refused = _cli(
        cfg,
        "claim",
        str(task1),
        "--takeover",
        "--branch",
        "feat/two",
        "--scope",
        "src/x/**",
        "--no-worktree",
        agent=c,
    )
    assert refused.returncode == 1
    assert "already claimed" in refused.stderr

    holder = work.active_claim_for_task(conn, task1)
    assert holder is not None and holder["agent"] == a, (
        "refused takeover must not release the holder"
    )
    assert gate.get_approval(conn, gate.operation_hash(operation))["status"] == "approved", (
        "refused takeover must not consume the approval"
    )
    assert work.get_task(conn, task1)["status"] == "claimed"
    conn.commit()

    taken = _cli(
        cfg,
        "claim",
        str(task1),
        "--takeover",
        "--branch",
        "feat/one-b",
        "--scope",
        "src/x/**",
        "--no-worktree",
        agent=c,
    )
    assert taken.returncode == 0, taken.stderr
    holder = work.active_claim_for_task(conn, task1)
    assert holder is not None and holder["agent"] == c
    assert gate.get_approval(conn, gate.operation_hash(operation))["status"] == "denied"


def test_task_lock_serializes_release_with_takeover_check(initialized_store, tmp_path):
    """The incumbent cannot change while a takeover binds its approval."""
    repo_path = make_git_repo(tmp_path / "locked-repo")
    suffix = uuid.uuid4().hex[:8]
    setup = store.connect(initialized_store)
    repo_name = f"locked-{suffix}"
    registry.add_repository(setup, repo_name, repo_path)
    owner = f"owner-{suffix}"
    registry.add_agent(setup, owner)
    task = work.create_task(setup, repo_name, "locked takeover")
    claim, _, _ = work.claim_task(
        setup, task, owner, branch="feat/locked", scope_globs=["src/**"]
    )
    setup.commit()
    setup.close()

    locker = store.connect(initialized_store)
    releaser = store.connect(initialized_store)
    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []

    def release_in_parallel():
        started.set()
        try:
            work.release_claim(releaser, claim, owner)
            releaser.commit()
        except Exception as exc:
            errors.append(exc)
            releaser.rollback()
        finally:
            finished.set()

    try:
        work.lock_task(locker, task)
        thread = threading.Thread(target=release_in_parallel)
        thread.start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=0.5), (
            "release bypassed the task ownership lock"
        )
        locker.commit()
        thread.join(timeout=30)
        assert finished.is_set() and not errors
    finally:
        locker.close()
        releaser.close()


# ------------------------------------------------------------ finding 4


def _addressed_handoff(conn, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"decl-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    a, b, c = f"a-{suffix}", f"b-{suffix}", f"c-{suffix}"
    for name in (a, b, c):
        registry.add_agent(conn, name)
    task = work.create_task(conn, repo, "addressed work")
    claim_a, _, _ = work.claim_task(
        conn, task, a, branch="feat/x", scope_globs=["src/**"]
    )
    hid = handoff.create_handoff(
        conn, claim_a, a, _handoff_record(git_repo), to_agent=b
    )
    return task, hid, a, b, c


def test_decline_requires_registered_addressee(conn, git_repo):
    task, hid, a, b, c = _addressed_handoff(conn, git_repo)

    with pytest.raises(RegistryError, match="not registered"):
        handoff.decline_handoff(conn, hid, "unregistered-intruder")
    with pytest.raises(handoff.HandoffError, match="only the addressee"):
        handoff.decline_handoff(conn, hid, c)
    with pytest.raises(handoff.HandoffError, match="only the addressee"):
        handoff.decline_handoff(conn, hid, a)
    assert handoff.get_handoff(conn, hid)["status"] == "open"

    handoff.decline_handoff(conn, hid, b)
    assert handoff.get_handoff(conn, hid)["status"] == "declined"


def test_cancel_is_creator_only(conn, git_repo):
    task, hid, a, b, c = _addressed_handoff(conn, git_repo)

    with pytest.raises(handoff.HandoffError, match="only the creator"):
        handoff.cancel_handoff(conn, hid, b)
    handoff.cancel_handoff(conn, hid, a)
    assert handoff.get_handoff(conn, hid)["status"] == "cancelled"
    assert work.get_task(conn, task)["status"] == "open"

    new_claim, _, _ = work.claim_task(
        conn, task, c, branch="feat/fresh", scope_globs=["src/**"]
    )
    assert new_claim


def test_unaddressed_handoff_cannot_be_declined(conn, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"unaddr-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    a, b = f"a-{suffix}", f"b-{suffix}"
    registry.add_agent(conn, a)
    registry.add_agent(conn, b)
    task = work.create_task(conn, repo, "open offer")
    claim_a, _, _ = work.claim_task(
        conn, task, a, branch="feat/x", scope_globs=["src/**"]
    )
    hid = handoff.create_handoff(conn, claim_a, a, _handoff_record(git_repo))

    with pytest.raises(handoff.HandoffError, match="unaddressed"):
        handoff.decline_handoff(conn, hid, b)
    handoff.cancel_handoff(conn, hid, a)
    assert handoff.get_handoff(conn, hid)["status"] == "cancelled"


def _race_accept_and_decline(initialized_store, git_repo, winner):
    setup = store.connect(initialized_store)
    task, hid, _, addressee, _ = _addressed_handoff(setup, git_repo)
    setup.commit()
    setup.close()

    first = store.connect(initialized_store)
    second = store.connect(initialized_store)
    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []

    if winner == "accepted":
        handoff.accept_handoff(first, hid, addressee)
        losing_status = "accepted"
    else:
        handoff.decline_handoff(first, hid, addressee)
        losing_status = "declined"

    def losing_action():
        if winner == "accepted":
            handoff.decline_handoff(second, hid, addressee)
        else:
            handoff.accept_handoff(second, hid, addressee)

    def resolve_in_parallel():
        started.set()
        try:
            losing_action()
            second.commit()
        except Exception as exc:
            errors.append(exc)
            second.rollback()
        finally:
            finished.set()

    try:
        thread = threading.Thread(target=resolve_in_parallel)
        thread.start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=0.5), (
            "losing resolution bypassed the handoff writer reservation"
        )
        first.commit()
        thread.join(timeout=30)
        assert finished.is_set(), "concurrent handoff resolution deadlocked"
        assert len(errors) == 1
        assert isinstance(errors[0], handoff.HandoffError)
        assert f"is {losing_status}" in str(errors[0])
        check = store.connect(initialized_store)
        try:
            resolved = handoff.get_handoff(check, hid)
            task_state = work.get_task(check, task)
            claim = work.active_claim_for_task(check, task)
            assert resolved["status"] == winner
            if winner == "accepted":
                assert task_state["status"] == "claimed"
                assert claim is not None and claim["agent"] == addressee
            else:
                assert task_state["status"] == "open"
                assert claim is None
        finally:
            check.close()
    finally:
        first.close()
        second.close()


def test_concurrent_accept_wins_over_decline(initialized_store, git_repo):
    _race_accept_and_decline(initialized_store, git_repo, "accepted")


def test_concurrent_decline_wins_over_accept(initialized_store, git_repo):
    _race_accept_and_decline(initialized_store, git_repo, "declined")


# ------------------------------------------------------------ finding 5


def _claim_with_worktree(conn, cfg, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"oid-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    agent = f"a-{suffix}"
    registry.add_agent(conn, agent)
    task = work.create_task(conn, repo, "verified continuation")
    claim_id, _, _ = work.claim_task(
        conn, task, agent, branch="feat/x", scope_globs=["src/**"]
    )
    trees.create_worktree(conn, claim_id, cfg.worktrees_dir)
    return claim_id, agent


def test_checkpoint_rejects_nonexistent_commit(conn, cfg, git_repo):
    claim_id, agent = _claim_with_worktree(conn, cfg, git_repo)
    with pytest.raises(work.WorkError, match="does not exist"):
        work.add_checkpoint(conn, claim_id, agent, "deadbeef" * 5)


def test_checkpoint_rejects_unreachable_commit(conn, cfg, git_repo):
    claim_id, agent = _claim_with_worktree(conn, cfg, git_repo)
    (git_repo / "unrelated.txt").write_text("elsewhere\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@localhost",
    }
    import os

    subprocess.run(
        ["git", "-C", str(git_repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "main moves"],
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )
    main_head = _head(git_repo)
    with pytest.raises(work.WorkError, match="not reachable"):
        work.add_checkpoint(conn, claim_id, agent, main_head)


def test_handoff_rejects_nonexistent_last_commit(conn, cfg, git_repo):
    claim_id, agent = _claim_with_worktree(conn, cfg, git_repo)
    record = {
        "completed": "part",
        "remaining": "rest",
        "last_commit": "deadbeef" * 5,
    }
    with pytest.raises(work.WorkError, match="does not exist"):
        handoff.create_handoff(conn, claim_id, agent, record)
