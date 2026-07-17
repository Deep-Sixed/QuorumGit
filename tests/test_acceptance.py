"""The QuorumGit completion contract, end to end.

Steps (numbered per the build contract):
 1. Register a Git repository.
 2. Register two distinct agents.
 3. Agent A claims a task.
 4. Agent A gets an isolated worktree.
 5. Agent A's expected write scope is recorded.
 6. Agent B is blocked from claiming overlapping work unknowingly.
 7. Agent B performs unrelated work in another worktree.
 8. Commits, progress, validation, and remaining work are recorded.
 9. A durable handoff is created from Agent A to Agent B.
10. Agent B accepts the handoff and continues from the correct commit.
11. The original lease is released/expired safely.
12. Force takeover requires explicit approval (this test exercises the
    lease-takeover path; protected-ref / force-push / deletion enforcement
    is covered by test_gate.py, which needs a bare hub).
13. An audit history of every claim, conflict, handoff, approval, release exists.
14. Restart testing preserves active task and handoff state.
15. No secondary-store, no legacy-brand dependencies in application code
    (store singularity: test_store.py::test_single_store_no_external_mode;
    branding: test_branding.py) — asserted there, not re-proven here.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

from quorumgit import gate, handoff, registry, store, trees, work
from tests.conftest import make_git_repo


def test_full_workflow(committed_conn, cfg, tmp_path, initialized_store):
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]

    # 1. register repository
    repo_path = make_git_repo(tmp_path / "project")
    repo_name = f"project-{suffix}"
    registry.add_repository(conn, repo_name, repo_path)

    # 2. register two agents
    agent_a, agent_b = f"a-{suffix}", f"b-{suffix}"
    registry.add_agent(conn, agent_a)
    registry.add_agent(conn, agent_b)

    # 3–5. A claims a task, gets an isolated worktree, scope recorded
    task_a = work.create_task(conn, repo_name, "implement feature",
                              objective="build the feature in src/")
    claim_a, classification, _ = work.claim_task(
        conn, task_a, agent_a, branch="feat/feature", scope_globs=["src/**"]
    )
    assert classification == "CLEAR"
    wt_a = trees.create_worktree(conn, claim_a, cfg.worktrees_dir)
    assert Path(wt_a["path"]).is_dir()
    scopes = [r[0] for r in conn.execute(
        "SELECT path_glob FROM scopes WHERE claim_id = %s", (claim_a,)
    ).fetchall()]
    assert scopes == ["src/**"]

    # 6. B is blocked from overlapping work unless deliberate
    task_b = work.create_task(conn, repo_name, "refactor src internals")
    with pytest.raises(work.ClaimRefused, match="overlap"):
        work.claim_task(conn, task_b, agent_b, branch="feat/refactor",
                        scope_globs=["src/app*"])

    # 7. B proceeds with unrelated work in its own worktree
    task_c = work.create_task(conn, repo_name, "write docs")
    claim_b, classification_b, _ = work.claim_task(
        conn, task_c, agent_b, branch="feat/docs", scope_globs=["docs/**"]
    )
    assert classification_b == "RELATED"
    wt_b = trees.create_worktree(conn, claim_b, cfg.worktrees_dir)
    assert wt_b["path"] != wt_a["path"]

    # 8. work happens in A's worktree; checkpoint records commit + progress
    wt_path = Path(wt_a["path"])
    (wt_path / "src" / "feature.py").write_text("def feature(): return 42\n")
    subprocess.run(
        ["git", "-C", str(wt_path), "add", "-A"], check=True,
        capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "-c", "user.name=t",
         "-c", "user.email=t@localhost", "commit", "-m", "feature: first cut"],
        check=True, capture_output=True)
    feature_commit = trees.head_commit(wt_path)
    work.add_checkpoint(conn, claim_a, agent_a, feature_commit,
                        note="first cut compiles")

    # 9. durable handoff A -> B
    handoff_id = handoff.create_handoff(
        conn, claim_a, agent_a,
        record={
            "completed": "feature skeleton implemented",
            "remaining": "edge cases and tests",
            "last_commit": feature_commit,
            "files_changed": ["src/feature.py"],
            "blockers": [],
            "validation": "manual run ok",
        },
        to_agent=agent_b,
    )

    # 11 (first half). the original lease was released safely by the handoff
    old_claim = work.get_claim(conn, claim_a)
    assert old_claim["released_at"] is not None
    assert old_claim["release_reason"] == "handoff"

    # 10. B accepts and continues from the exact commit
    result = handoff.accept_handoff(conn, handoff_id, agent_b)
    assert result["last_commit"] == feature_commit
    assert result["worktree"] == wt_a["path"]
    assert trees.head_commit(result["worktree"]) == feature_commit
    assert result["record"]["remaining"] == "edge cases and tests"

    # 11 (second half). B releases its docs lease cleanly
    work.release_claim(conn, claim_b, agent_b)

    # 12. force takeover of B's continued claim requires explicit approval
    takeover_op = {
        "type": "lease_takeover",
        "repository": repo_name,
        "task_id": task_a,
        "from_agent": agent_b,
        "to_agent": agent_a,
    }
    assert not gate.is_approved(conn, takeover_op)
    holder = work.active_claim_for_task(conn, task_a)
    assert holder is not None
    with pytest.raises(work.ClaimRefused):
        work.claim_task(conn, task_a, agent_a, branch="feat/feature-2",
                        scope_globs=["src/**"])
    gate.request_approval(conn, takeover_op, requested_by="operator")
    gate.vote(conn, gate.operation_hash(takeover_op), "operator", True)
    assert gate.is_approved(conn, takeover_op)
    gate.consume_approval(conn, takeover_op, agent=agent_a)
    claim_back, _, _ = work.claim_task(
        conn, task_a, agent_a, branch="feat/feature-2",
        scope_globs=["src/**"], takeover_approved=True,
    )
    assert claim_back
    released_b = work.get_claim(conn, holder["id"])
    assert released_b["released_at"] is not None

    # 13. audit history covers claims, conflicts, handoffs, approvals, releases
    events = {
        row[0]
        for row in conn.execute("SELECT DISTINCT event_type FROM audit_events")
    }
    for expected in (
        "repository.registered", "agent.registered", "task.created",
        "claim.acquired", "claim.released", "checkpoint.recorded",
        "worktree.created", "handoff.created", "handoff.accepted",
        "approval.requested", "approval.vote", "approval.approved",
        "approval.consumed",
    ):
        assert expected in events, f"missing audit event {expected}"
    conflicts = conn.execute(
        "SELECT count(*) FROM conflict_events WHERE classification = 'OVERLAPPING'"
    ).fetchone()[0]
    assert conflicts >= 1
    conn.commit()

    # 14. restart: active task/claim/handoff state survives
    store.stop(cfg)
    uri = store.ensure_running(cfg)
    store.verify_contract(uri)
    with psycopg.connect(uri) as conn2:
        assert work.get_task(conn2, task_a)["status"] == "claimed"
        surviving = work.active_claim_for_task(conn2, task_a)
        assert surviving and surviving["id"] == claim_back
        assert handoff.get_handoff(conn2, handoff_id)["status"] == "accepted"
