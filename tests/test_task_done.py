"""`task done`: the claim holder closes a task for good.

Completing releases the live claim and moves the task to the terminal `done`
status in one transaction; nobody can claim a done task afterwards.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import handoff, registry, trees, work
from tests.test_cli_hub import _cli


def _setup(conn, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"repo-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    a, b = f"a-{suffix}", f"b-{suffix}"
    for name in (a, b):
        registry.add_agent(conn, name)
    task = work.create_task(conn, repo, "finishable work")
    return task, a, b, suffix


def _claim(conn, task, agent, suffix):
    claim, _, _ = work.claim_task(
        conn, task, agent, branch=f"feat/{suffix}", scope_globs=[f"src/{suffix}/**"]
    )
    return claim


def test_holder_marks_task_done(conn, git_repo):
    task, a, _, suffix = _setup(conn, git_repo)
    claim = _claim(conn, task, a, suffix)

    released = work.complete_task(conn, task, a, note="shipped")

    assert released == claim
    assert work.get_task(conn, task)["status"] == "done"
    released_claim = work.get_claim(conn, claim)
    assert released_claim["released_at"] is not None
    assert released_claim["release_reason"] == "done"
    detail = conn.execute(
        "SELECT detail FROM audit_events WHERE event_type = 'task.done' "
        "AND entity = 'task' AND entity_id = ?",
        (task,),
    ).fetchone()
    assert detail is not None and '"note":"shipped"' in detail[0]


def test_done_task_cannot_be_claimed(conn, git_repo):
    task, a, b, suffix = _setup(conn, git_repo)
    _claim(conn, task, a, suffix)
    work.complete_task(conn, task, a)

    with pytest.raises(work.ClaimRefused, match="is done"):
        work.claim_task(conn, task, b, branch="feat/again", scope_globs=["docs/**"])
    with pytest.raises(work.WorkError, match="already done"):
        work.complete_task(conn, task, a)


def test_only_the_live_claim_holder_can_complete(conn, git_repo):
    task, a, b, suffix = _setup(conn, git_repo)

    with pytest.raises(work.WorkError, match="no live claim"):
        work.complete_task(conn, task, a)

    claim = _claim(conn, task, a, suffix)
    with pytest.raises(work.WorkError, match=f"claimed by {a}"):
        work.complete_task(conn, task, b)

    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 1 WHERE id = ?",
        (claim,),
    )
    with pytest.raises(work.WorkError, match="expired"):
        work.complete_task(conn, task, a)
    assert work.get_task(conn, task)["status"] == "claimed"


def test_open_handoff_must_be_accepted_first(conn, git_repo):
    task, a, b, suffix = _setup(conn, git_repo)
    claim = _claim(conn, task, a, suffix)
    head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    hid = handoff.create_handoff(
        conn, claim, a,
        {"completed": "part", "remaining": "rest", "last_commit": head},
        to_agent=b,
    )

    with pytest.raises(work.WorkError, match=f"open handoff {hid}"):
        work.complete_task(conn, task, a)

    handoff.accept_handoff(conn, hid, b)
    work.complete_task(conn, task, b)
    assert work.get_task(conn, task)["status"] == "done"


def _cli_claim(cfg, task, agent, suffix) -> tuple[int, Path]:
    claimed = _cli(
        cfg, "claim", str(task), "--branch", f"feat/{suffix}",
        "--scope", f"src/{suffix}/**", agent=agent,
    )
    assert claimed.returncode == 0, claimed.stderr
    claim = re.search(r"claim (\d+) acquired", claimed.stdout)
    path = re.search(r"worktree: (.+)", claimed.stdout)
    assert claim is not None and path is not None
    return int(claim.group(1)), Path(path.group(1).strip())


def test_cli_done_removes_clean_worktree(committed_conn, git_repo, cfg):
    conn = committed_conn
    task, a, _, suffix = _setup(conn, git_repo)
    conn.commit()
    claim, path = _cli_claim(cfg, task, a, suffix)

    done = _cli(cfg, "task", "done", str(task), "--remove-worktree", agent=a)

    assert done.returncode == 0, done.stderr
    assert f"task {task} done (claim {claim} released)" in done.stdout
    assert "worktree removed" in done.stdout
    assert not path.exists()
    wt = trees.worktree_for_claim(conn, claim)
    assert wt is not None and wt["removed_at"] is not None
    listed = _cli(cfg, "task", "list", agent=a)
    assert f"{task}\t" in listed.stdout and "\tdone\t" in listed.stdout


def test_cli_done_refuses_dirty_worktree_and_changes_nothing(
    committed_conn, git_repo, cfg
):
    conn = committed_conn
    task, a, _, suffix = _setup(conn, git_repo)
    conn.commit()
    claim, path = _cli_claim(cfg, task, a, suffix)
    (path / "uncommitted.txt").write_text("work in progress\n")

    refused = _cli(cfg, "task", "done", str(task), "--remove-worktree", agent=a)

    assert refused.returncode == 1
    assert path.exists() and (path / "uncommitted.txt").exists()
    # The whole command rolled back: the task is still claimed by its holder.
    assert work.get_task(conn, task)["status"] == "claimed"
    assert work.get_claim(conn, claim)["released_at"] is None

    # Without --remove-worktree the task closes and the checkout is kept.
    done = _cli(cfg, "task", "done", str(task), agent=a)
    assert done.returncode == 0, done.stderr
    assert "worktree removed" not in done.stdout
    assert (path / "uncommitted.txt").exists()
