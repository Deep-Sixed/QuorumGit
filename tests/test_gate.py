"""Phase 3: approval gate and pre-receive enforcement against real pushes."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from quorumgit import gate, registry, work
from tests.conftest import make_git_repo


def _setup(committed_conn, tmp_path):
    """A bare hub with the hook installed, plus a clone to push from."""
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]
    seed = make_git_repo(tmp_path / "seed")
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(hub)],
                   check=True, capture_output=True)
    repo_name = f"hub-{suffix}"
    registry.add_repository(conn, repo_name, hub,
                            protected_refs=["refs/heads/main"])
    a, b = f"agent-a-{suffix}", f"agent-b-{suffix}"
    registry.add_agent(conn, a)
    registry.add_agent(conn, b)
    gate.install_hook(conn, repo_name)
    conn.commit()

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(hub), str(clone)],
                   check=True, capture_output=True)
    return repo_name, hub, clone, a, b


def _push(clone: Path, agent: str | None, *refspec: str, cfg=None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
    }
    env.pop("QUORUMGIT_AGENT", None)
    if agent:
        env["QUORUMGIT_AGENT"] = agent
    if cfg:
        env["QUORUMGIT_DATA_DIR"] = str(cfg.data_dir)
        env["QUORUMGIT_PG_INSTANCE"] = cfg.pg_instance
        env["QUORUMGIT_PG_PORT"] = str(cfg.pg_port)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    return subprocess.run(
        ["git", "-C", str(clone), "push", "origin", *refspec],
        capture_output=True, text=True, env=env,
    )


def _commit(clone: Path, filename: str, branch: str | None = None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
    }

    def git(*args):
        subprocess.run(["git", "-C", str(clone), *args], check=True,
                       capture_output=True, env=env)

    if branch:
        git("checkout", "-B", branch)
    (clone / filename).write_text("change\n")
    git("add", "-A")
    git("commit", "-m", f"edit {filename}")


def test_operation_hash_requires_fields():
    with pytest.raises(gate.GateError):
        gate.operation_hash({"type": "x"})


def test_vote_threshold_and_deny(conn):
    op = {"type": "test_op", "repository": "r", "n": 1}
    approval = gate.request_approval(conn, op, requested_by="op", threshold=2)
    assert approval["status"] == "pending"
    gate.vote(conn, approval["operation_hash"], "voter-1", True)
    assert gate.get_approval(conn, approval["operation_hash"])["status"] == "pending"
    result = gate.vote(conn, approval["operation_hash"], "voter-2", True)
    assert result["status"] == "approved"
    assert gate.is_approved(conn, op)

    op2 = {"type": "test_op", "repository": "r", "n": 2}
    approval2 = gate.request_approval(conn, op2, requested_by="op")
    result2 = gate.vote(conn, approval2["operation_hash"], "voter-1", False)
    assert result2["status"] == "denied"
    assert not gate.is_approved(conn, op2)


def test_consume_approval_is_single_use(conn):
    """The same approval payload cannot authorize two operations."""
    op = {"type": "protected_ref_update", "repository": "r", "n": 1}
    gate.request_approval(conn, op, requested_by="op")
    gate.vote(conn, gate.operation_hash(op), "op", True)
    assert gate.is_approved(conn, op)
    gate.consume_approval(conn, op, agent="pusher-1")
    assert not gate.is_approved(conn, op)
    # A second consume of the identical payload must fail, not silently pass.
    with pytest.raises(gate.GateError, match="not consumable"):
        gate.consume_approval(conn, op, agent="pusher-2")


def test_consume_approval_concurrent_single_winner(initialized_store):
    """Two live connections race to consume one approval; exactly one wins.

    The second consumer is already in flight (blocked on the FOR UPDATE row
    lock) before the first commits, so this exercises genuine concurrency,
    not sequential reuse.
    """
    op = {"type": "protected_ref_update", "repository": "race-repo",
          "n": uuid.uuid4().hex}
    with psycopg.connect(initialized_store) as setup:
        gate.request_approval(setup, op, requested_by="op")
        gate.vote(setup, gate.operation_hash(op), "op", True)
        setup.commit()

    conn1 = psycopg.connect(initialized_store)
    conn2 = psycopg.connect(initialized_store)
    racer_error: list[Exception] = []
    racer_started = threading.Event()

    def racer():
        racer_started.set()
        try:
            gate.consume_approval(conn2, op, agent="pusher-2")
            conn2.commit()
        except gate.GateError as exc:
            racer_error.append(exc)
            conn2.rollback()

    try:
        # First consumer holds the row lock, uncommitted.
        gate.consume_approval(conn1, op, agent="pusher-1")
        thread = threading.Thread(target=racer)
        thread.start()
        racer_started.wait(timeout=10)
        time.sleep(0.5)  # let the racer block on the row lock
        conn1.commit()  # release the lock; the racer must now lose
        thread.join(timeout=30)
        assert not thread.is_alive(), "second consumer never returned"
        assert racer_error, "both consumers succeeded — approval used twice"

        with psycopg.connect(initialized_store) as check:
            approval = gate.get_approval(check, gate.operation_hash(op))
            assert approval["status"] == "denied"
            consumed = check.execute(
                "SELECT count(*) FROM audit_events "
                "WHERE event_type = 'approval.consumed' AND entity_id = %s",
                (approval["id"],),
            ).fetchone()
            assert consumed is not None and consumed[0] == 1
    finally:
        conn1.close()
        conn2.close()


def test_hook_protected_ref_requires_approval(committed_conn, tmp_path, cfg):
    repo_name, hub, clone, a, b = _setup(committed_conn, tmp_path)
    _commit(clone, "newfile.txt")
    result = _push(clone, a, "main", cfg=cfg)
    assert result.returncode != 0
    assert "requires an approval" in result.stderr

    # approve the exact update, then it lands
    oldrev = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.strip()
    newrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    op = {"type": "protected_ref_update", "repository": repo_name,
          "refname": "refs/heads/main", "oldrev": oldrev, "newrev": newrev}
    gate.request_approval(committed_conn, op, requested_by="operator")
    gate.vote(committed_conn, gate.operation_hash(op), "operator", True)
    committed_conn.commit()

    result = _push(clone, a, "main", cfg=cfg)
    assert result.returncode == 0, result.stderr

    # the approval was consumed: replaying the same update is refused
    subprocess.run(["git", "-C", str(clone), "reset", "--hard", oldrev],
                   check=True, capture_output=True)
    force = _push(clone, a, "+main", cfg=cfg)
    assert force.returncode != 0  # force update needs its own approval


def test_hook_claimed_branch_rejects_other_agents(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "guarded work")
    work.claim_task(conn, task, a, branch="feat/guarded",
                    scope_globs=["src/**"])
    conn.commit()

    _commit(clone, "src/change.py", branch="feat/guarded")
    stranger = _push(clone, b, "feat/guarded", cfg=cfg)
    assert stranger.returncode != 0
    assert "claimed by" in stranger.stderr

    anonymous = _push(clone, None, "feat/guarded", cfg=cfg)
    assert anonymous.returncode != 0

    owner = _push(clone, a, "feat/guarded", cfg=cfg)
    assert owner.returncode == 0, owner.stderr

    # unclaimed branches remain open to anyone
    _commit(clone, "docs/free.md", branch="feat/free")
    free = _push(clone, b, "feat/free", cfg=cfg)
    assert free.returncode == 0, free.stderr
