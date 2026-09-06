"""Regression coverage for ownership, handoff, and worktree integrity."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from quorumgit import handoff, registry, store, trees, work


def _setup(conn, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"integrity-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    agents = [f"agent-{name}-{suffix}" for name in ("a", "b", "c")]
    for agent in agents:
        registry.add_agent(conn, agent)
    return repo, agents[0], agents[1], agents[2]


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _handoff_record(repo: Path) -> dict:
    return {
        "completed": "completed work",
        "remaining": "remaining work",
        "last_commit": _head(repo),
    }


@pytest.mark.parametrize("lease", [0.0, -1.0, float("nan"), float("inf")])
def test_claim_rejects_invalid_lease(conn, git_repo, lease):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "invalid lease")
    with pytest.raises(work.ClaimRefused, match="Lease duration"):
        work.claim_task(
            conn,
            task,
            a,
            branch="feat/invalid-lease",
            scope_globs=["src/**"],
            lease_hours=lease,
        )
    assert work.active_claim_for_task(conn, task) is None


def test_renew_rejects_invalid_lease(conn, git_repo):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "renew lease")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/renew", scope_globs=["src/**"]
    )
    with pytest.raises(work.WorkError, match="Lease duration"):
        work.renew_claim(conn, claim, a, lease_hours=0)


def test_expired_claim_cannot_renew(conn, git_repo):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "expired renewal")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/expired", scope_globs=["src/**"]
    )
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 60 WHERE id = ?",
        (claim,),
    )
    with pytest.raises(work.WorkError, match="expired"):
        work.renew_claim(conn, claim, a)


def test_expired_claim_does_not_resurrect_cross_task_branch(conn, git_repo):
    repo, a, b, _c = _setup(conn, git_repo)
    first = work.create_task(conn, repo, "first")
    second = work.create_task(conn, repo, "second")
    old, _, _ = work.claim_task(
        conn, first, a, branch="feat/shared", scope_globs=["src/**"]
    )
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 60 WHERE id = ?",
        (old,),
    )
    new, _, _ = work.claim_task(
        conn, second, b, branch="feat/shared", scope_globs=["docs/**"]
    )
    with pytest.raises(work.WorkError, match="expired"):
        work.renew_claim(conn, old, a)
    assert work.get_claim(conn, new)["agent"] == b
    live = work.live_claim_for_branch(
        conn, work.get_task(conn, second)["repository_id"], "feat/shared"
    )
    assert live is not None and live["id"] == new


def test_open_handoff_reserves_branch_across_tasks(conn, git_repo):
    repo, a, b, c = _setup(conn, git_repo)
    first = work.create_task(conn, repo, "handoff source")
    second = work.create_task(conn, repo, "other task")
    claim, _, _ = work.claim_task(
        conn, first, a, branch="feat/reserved", scope_globs=["src/**"]
    )
    handoff.create_handoff(conn, claim, a, _handoff_record(git_repo), to_agent=b)
    with pytest.raises(work.ClaimRefused, match="reserved for open handoff"):
        work.claim_task(
            conn,
            second,
            c,
            branch="feat/reserved",
            scope_globs=["docs/**"],
        )


def test_open_handoff_reserves_scopes_across_tasks(conn, git_repo):
    repo, a, b, c = _setup(conn, git_repo)
    first = work.create_task(conn, repo, "handoff source")
    second = work.create_task(conn, repo, "other task")
    claim, _, _ = work.claim_task(
        conn, first, a, branch="feat/source", scope_globs=["src/**"]
    )
    handoff.create_handoff(conn, claim, a, _handoff_record(git_repo), to_agent=b)
    with pytest.raises(work.ClaimRefused, match="reserved by open handoff"):
        work.claim_task(
            conn,
            second,
            c,
            branch="feat/other",
            scope_globs=["src/app*"],
            override_overlap=True,
        )


def test_unauthorized_remove_does_not_touch_worktree(conn, git_repo, cfg):
    repo, a, b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "owned checkout")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/owned", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    with pytest.raises(trees.WorktreeError, match="belongs to"):
        trees.remove_worktree(conn, claim, b)
    assert Path(wt["path"]).exists()
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is None


def test_same_task_same_agent_can_create_new_worktree_after_release(
    conn, git_repo, cfg
):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "repeat checkout")
    first, _, _ = work.claim_task(
        conn, task, a, branch="feat/repeat", scope_globs=["src/**"]
    )
    wt1 = trees.create_worktree(conn, first, cfg.worktrees_dir)
    trees.remove_worktree(conn, first, a)
    work.release_claim(conn, first, a)

    second, _, _ = work.claim_task(
        conn, task, a, branch="feat/repeat", scope_globs=["src/**"]
    )
    wt2 = trees.create_worktree(conn, second, cfg.worktrees_dir)
    assert wt2["path"] != wt1["path"]
    assert Path(wt2["path"]).exists()


def test_decline_cleans_retained_handoff_worktree(conn, git_repo, cfg):
    repo, a, b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "decline cleanup")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/decline", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    hid = handoff.create_handoff(
        conn, claim, a, _handoff_record(git_repo), to_agent=b
    )
    handoff.decline_handoff(conn, hid, b)
    assert not Path(wt["path"]).exists()
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is not None
    assert handoff.get_handoff(conn, hid)["status"] == "declined"


def test_cancel_cleans_retained_handoff_worktree(conn, git_repo, cfg):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "cancel cleanup")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/cancel", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    hid = handoff.create_handoff(conn, claim, a, _handoff_record(git_repo))
    handoff.cancel_handoff(conn, hid, a)
    assert not Path(wt["path"]).exists()
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is not None
    assert handoff.get_handoff(conn, hid)["status"] == "cancelled"


def test_dirty_handoff_worktree_is_not_force_removed(conn, git_repo, cfg):
    repo, a, b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "dirty cleanup")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/dirty", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    (Path(wt["path"]) / "dirty.txt").write_text("do not delete\n")
    hid = handoff.create_handoff(
        conn, claim, a, _handoff_record(git_repo), to_agent=b
    )
    with pytest.raises(trees.WorktreeError):
        handoff.decline_handoff(conn, hid, b)
    assert Path(wt["path"]).exists()
    assert handoff.get_handoff(conn, hid)["status"] == "open"


def test_doctor_repairs_missing_recorded_worktree(conn, git_repo, cfg):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "doctor missing")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/missing", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    shutil.rmtree(wt["path"])
    findings = trees.doctor_worktrees(conn)
    assert any(f["issue"] == "missing" for f in findings)
    repaired = trees.doctor_worktrees(conn, repair=True)
    assert any(f["issue"] == "missing" and f["repaired"] for f in repaired)
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is not None


def test_doctor_removes_orphaned_released_worktree(conn, git_repo, cfg):
    repo, a, _b, _c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "doctor orphan")
    claim, _, _ = work.claim_task(
        conn, task, a, branch="feat/orphan", scope_globs=["src/**"]
    )
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    work.release_claim(conn, claim, a)
    findings = trees.doctor_worktrees(conn)
    assert any(f["issue"] == "orphaned" for f in findings)
    repaired = trees.doctor_worktrees(conn, repair=True)
    assert any(f["issue"] == "orphaned" and f["repaired"] for f in repaired)
    assert not Path(wt["path"]).exists()
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is not None


def test_cross_process_same_branch_has_one_winner(
    committed_conn, initialized_store, git_repo, tmp_path
):
    repo, a, b, _c = _setup(committed_conn, git_repo)
    first = work.create_task(committed_conn, repo, "race one")
    second = work.create_task(committed_conn, repo, "race two")
    committed_conn.commit()

    marker = tmp_path / "writer-started"
    script = tmp_path / "claim_worker.py"
    script.write_text(
        """
from pathlib import Path
import sys, time
from quorumgit import store, work
from quorumgit.config import Config
cfg = Config(data_dir=Path(sys.argv[1]), agent=None)
task = int(sys.argv[2]); agent = sys.argv[3]; mode = sys.argv[4]
conn = store.connect(cfg)
try:
    try:
        work.claim_task(conn, task, agent, branch='feat/process-race', scope_globs=['src/**'])
        if mode == 'holder':
            Path(sys.argv[5]).write_text('ready')
            time.sleep(0.8)
        conn.commit()
        print('ACQUIRED')
    except work.ClaimRefused:
        conn.rollback()
        print('REFUSED')
finally:
    conn.close()
""".lstrip(),
        encoding="utf-8",
    )
    data_dir = str(initialized_store.data_dir)
    holder = subprocess.Popen(
        [sys.executable, str(script), data_dir, str(first), a, "holder", str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "holder never acquired the writer reservation"
    contender = subprocess.Popen(
        [sys.executable, str(script), data_dir, str(second), b, "contender", str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out1, err1 = holder.communicate(timeout=20)
    out2, err2 = contender.communicate(timeout=20)
    assert holder.returncode == 0, err1
    assert contender.returncode == 0, err2
    assert {out1.strip(), out2.strip()} == {"ACQUIRED", "REFUSED"}

    check = store.connect(initialized_store)
    try:
        row = check.execute(
            """
            SELECT count(*)
            FROM claims c JOIN tasks t ON t.id = c.task_id
            WHERE t.repository_id = ? AND c.branch = 'feat/process-race'
              AND c.released_at IS NULL AND c.lease_expires_at >= unixepoch()
            """,
            (work.get_task(check, first)["repository_id"],),
        ).fetchone()
        assert row is not None and row[0] == 1
    finally:
        check.close()
