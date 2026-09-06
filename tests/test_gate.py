"""Approval gate and pre-receive enforcement against real pushes."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, registry, store, work
from tests.conftest import make_git_repo


def _register_agents(conn, *names):
    for name in names:
        conn.execute("INSERT INTO agents (name) VALUES (?) ON CONFLICT DO NOTHING", (name,))


def _setup(committed_conn, tmp_path):
    """A bare hub with the hook installed, plus a clone to push from."""
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]
    seed = make_git_repo(tmp_path / "seed")
    hub = tmp_path / "hub.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(hub)],
        check=True,
        capture_output=True,
    )
    repo_name = f"hub-{suffix}"
    registry.add_repository(
        conn, repo_name, hub, protected_refs=["refs/heads/main"]
    )
    a, b = f"agent-a-{suffix}", f"agent-b-{suffix}"
    registry.add_agent(conn, a)
    registry.add_agent(conn, b)
    gate.install_hook(conn, repo_name)
    conn.commit()

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(hub), str(clone)], check=True, capture_output=True
    )
    return repo_name, hub, clone, a, b


def _push(clone: Path, agent: str | None, *refspec: str, cfg=None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@localhost",
    }
    env.pop("QUORUMGIT_AGENT", None)
    if agent:
        env["QUORUMGIT_AGENT"] = agent
    if cfg:
        env["QUORUMGIT_DATA_DIR"] = str(cfg.data_dir)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    return subprocess.run(
        ["git", "-C", str(clone), "push", "origin", *refspec],
        capture_output=True,
        text=True,
        env=env,
    )


def _commit(clone: Path, filename: str, branch: str | None = None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@localhost",
    }

    def git(*args):
        subprocess.run(
            ["git", "-C", str(clone), *args],
            check=True,
            capture_output=True,
            env=env,
        )

    if branch:
        git("checkout", "-B", branch)
    (clone / filename).write_text("change\n")
    git("add", "-A")
    git("commit", "-m", f"edit {filename}")


def test_operation_hash_requires_fields():
    with pytest.raises(gate.GateError):
        gate.operation_hash({"type": "x"})


def test_vote_threshold_and_deny(conn):
    _register_agents(conn, "op", "voter-1", "voter-2")
    op = {"type": "test_op", "repository": "r", "n": 1}
    approval = gate.request_approval(conn, op, requested_by="op", threshold=2)
    assert approval["status"] == "pending"
    gate.vote(conn, approval["id"], "voter-1", True)
    assert gate.get_approval(conn, approval["operation_hash"])["status"] == "pending"
    result = gate.vote(conn, approval["id"], "voter-2", True)
    assert result["status"] == "approved"
    assert gate.is_approved(conn, op)

    op2 = {"type": "test_op", "repository": "r", "n": 2}
    approval2 = gate.request_approval(conn, op2, requested_by="op")
    result2 = gate.vote(conn, approval2["id"], "voter-1", False)
    assert result2["status"] == "denied"
    assert not gate.is_approved(conn, op2)


def test_consume_approval_is_single_use(conn):
    """One approval instance cannot authorize two operations."""
    op = {"type": "protected_ref_update", "repository": "r", "n": 1}
    _register_agents(conn, "op", "pusher-1", "pusher-2")
    approval = gate.request_approval(conn, op, requested_by="op")
    gate.vote(conn, approval["id"], "op", True)
    assert gate.is_approved(conn, op)
    gate.consume_approval(conn, approval["id"], op, agent="pusher-1")
    consumed = gate.get_approval(conn, gate.operation_hash(op))
    assert consumed["status"] == "consumed"
    assert consumed["consumed_at"] is not None
    assert not gate.is_approved(conn, op)
    with pytest.raises(gate.GateError, match="not consumable"):
        gate.consume_approval(conn, approval["id"], op, agent="pusher-2")


def test_consumed_operation_can_be_approved_again(conn):
    """A consumed exact takeover may be requested again as a new instance."""
    op = {
        "type": "repeatable_test_operation",
        "repository": "r",
        "task_id": 7,
        "from_agent": "a",
        "to_agent": "b",
    }
    _register_agents(conn, "operator", "b")
    first = gate.request_approval(conn, op, requested_by="operator")
    gate.vote(conn, first["id"], "operator", True)
    gate.consume_approval(conn, first["id"], op, agent="b")
    assert gate.get_approval(conn, gate.operation_hash(op))["status"] == "consumed"

    second = gate.request_approval(conn, op, requested_by="operator")
    assert second["id"] != first["id"]
    assert second["status"] == "pending"
    gate.vote(conn, second["id"], "operator", True)
    assert gate.is_approved(conn, op)


def test_consume_approval_concurrent_single_winner(initialized_store):
    """Two real libSQL connections race to consume one approval; one wins.

    The second consumer is already in flight and blocked on SQLite's writer
    reservation before the first commits. This preserves the original genuine
    two-connection race while asserting BEGIN IMMEDIATE semantics.
    """
    op = {
        "type": "protected_ref_update",
        "repository": "race-repo",
        "n": uuid.uuid4().hex,
    }
    setup = store.connect(initialized_store)
    _register_agents(setup, "op", "pusher-1", "pusher-2")
    approval = gate.request_approval(setup, op, requested_by="op")
    gate.vote(setup, approval["id"], "op", True)
    setup.commit()
    setup.close()

    conn1 = store.connect(initialized_store)
    racer_error: list[Exception] = []
    racer_started = threading.Event()

    def racer():
        conn2 = store.connect(initialized_store)
        racer_started.set()
        try:
            gate.consume_approval(conn2, approval["id"], op, agent="pusher-2")
            conn2.commit()
        except gate.GateError as exc:
            racer_error.append(exc)
            conn2.rollback()
        finally:
            conn2.close()

    try:
        gate.consume_approval(conn1, approval["id"], op, agent="pusher-1")
        thread = threading.Thread(target=racer)
        thread.start()
        racer_started.wait(timeout=10)
        time.sleep(0.5)
        conn1.commit()
        thread.join(timeout=30)
        assert not thread.is_alive(), "second consumer never returned"
        assert racer_error, "both consumers succeeded — approval used twice"

        check = store.connect(initialized_store)
        try:
            approval = gate.get_approval(check, gate.operation_hash(op))
            assert approval["status"] == "consumed"
            consumed = check.execute(
                "SELECT count(*) FROM audit_events "
                "WHERE event_type = 'approval.consumed' AND entity_id = ?",
                (approval["id"],),
            ).fetchone()
            assert consumed is not None and consumed[0] == 1
        finally:
            check.close()
    finally:
        conn1.close()


def test_hook_protected_ref_requires_approval(committed_conn, tmp_path, cfg):
    repo_name, hub, clone, a, _b = _setup(committed_conn, tmp_path)
    _commit(clone, "newfile.txt")
    result = _push(clone, a, "main", cfg=cfg)
    assert result.returncode != 0
    assert "requires an approval" in result.stderr

    oldrev = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
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
        "refname": "refs/heads/main",
        "oldrev": oldrev,
        "newrev": newrev,
    }
    _register_agents(committed_conn, "operator")
    approval = gate.request_approval(committed_conn, op, requested_by="operator")
    gate.vote(committed_conn, approval["id"], "operator", True)
    committed_conn.commit()

    result = _push(clone, a, "main", cfg=cfg)
    assert result.returncode == 0, result.stderr

    subprocess.run(
        ["git", "-C", str(clone), "reset", "--hard", oldrev],
        check=True,
        capture_output=True,
    )
    force = _push(clone, a, "+main", cfg=cfg)
    assert force.returncode != 0


def test_hook_claimed_branch_rejects_other_agents(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "guarded work")
    work.claim_task(
        conn, task, a, branch="feat/guarded", scope_globs=["src/**"]
    )
    conn.commit()

    _commit(clone, "src/change.py", branch="feat/guarded")
    stranger = _push(clone, b, "feat/guarded", cfg=cfg)
    assert stranger.returncode != 0
    assert "claimed by" in stranger.stderr

    anonymous = _push(clone, None, "feat/guarded", cfg=cfg)
    assert anonymous.returncode != 0

    owner = _push(clone, a, "feat/guarded", cfg=cfg)
    assert owner.returncode == 0, owner.stderr

    _commit(clone, "docs/free.md", branch="feat/free")
    free = _push(clone, b, "feat/free", cfg=cfg)
    assert free.returncode == 0, free.stderr
