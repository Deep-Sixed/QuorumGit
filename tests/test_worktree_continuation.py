"""A superseding claim continues the retained checkout instead of colliding.

Git refuses to check one branch out twice. When a claim on the same task and
branch follows lease expiry, an approved takeover, or a release that kept its
worktree, the retained checkout (including uncommitted work) is verified and
transferred rather than duplicated. Managed paths never contain names.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from quorumgit import audit, registry, trees, work
from tests.conftest import approve, ensure_agent, make_git_repo
from tests.test_cli_hub import _cli


def _repo_and_agents(conn, tmp_path, *agents):
    suffix = uuid.uuid4().hex[:8]
    repo = f"continue-{suffix}"
    registry.add_repository(conn, repo, make_git_repo(tmp_path / "repo"))
    names = [f"{agent}-{suffix}" for agent in agents]
    for name in names:
        ensure_agent(conn, name)
    return repo, names


def _cli_claim(cfg, task, agent, branch, *extra):
    return _cli(
        cfg, "claim", str(task), "--branch", branch, "--scope", "src/**", *extra,
        agent=agent,
    )


def _claimed_worktree(result) -> Path:
    match = re.search(r"^worktree: (.+)$", result.stdout, re.MULTILINE)
    assert match is not None, result.stdout + result.stderr
    return Path(match.group(1))


def _expire(conn, task):
    conn.execute(
        "UPDATE claims SET lease_expires_at = unixepoch() - 1 "
        "WHERE task_id = ? AND released_at IS NULL",
        (task,),
    )


def _events(conn, event_type):
    return [
        event
        for event in audit.events(conn, entity="worktree", limit=1000)
        if event["event_type"] == event_type
    ]


def test_reclaim_after_expiry_continues_retained_worktree(
    committed_conn, cfg, tmp_path
):
    conn = committed_conn
    repo, (first, second) = _repo_and_agents(conn, tmp_path, "first", "second")
    task = work.create_task(conn, repo, "expiring work")
    conn.commit()

    claimed = _cli_claim(cfg, task, first, "feat/expiring")
    assert claimed.returncode == 0, claimed.stderr
    path = _claimed_worktree(claimed)
    (path / "src" / "unfinished.py").write_text("work in progress\n")

    _expire(conn, task)
    conn.commit()
    reclaimed = _cli_claim(cfg, task, second, "feat/expiring")
    assert reclaimed.returncode == 0, reclaimed.stderr
    assert _claimed_worktree(reclaimed) == path
    assert "continued retained worktree" in reclaimed.stdout
    assert (path / "src" / "unfinished.py").read_text() == "work in progress\n"

    holder = work.active_claim_for_task(conn, task)
    assert holder is not None and holder["agent"] == second
    wt = trees.worktree_for_claim(conn, holder["id"])
    assert wt is not None and wt["path"] == str(path)
    continued = [
        e for e in _events(conn, "worktree.continued")
        if e["detail"]["to_claim_id"] == holder["id"]
    ]
    assert continued and continued[0]["detail"]["from_release_reason"] == "lease_expired"


def test_governed_takeover_continues_incumbent_worktree(
    committed_conn, cfg, tmp_path
):
    conn = committed_conn
    repo, (holder, taker, requester) = _repo_and_agents(
        conn, tmp_path, "holder", "taker", "requester"
    )
    task = work.create_task(conn, repo, "taken work")
    conn.commit()
    claimed = _cli_claim(cfg, task, holder, "feat/taken")
    assert claimed.returncode == 0, claimed.stderr
    path = _claimed_worktree(claimed)

    incumbent = work.active_claim_for_task(conn, task)
    assert incumbent is not None
    approve(
        conn,
        {
            "type": "lease_takeover",
            "repository": repo,
            "task_id": task,
            "from_claim_id": incumbent["id"],
            "from_agent": holder,
            "to_agent": taker,
        },
        requested_by=requester,
    )
    conn.commit()

    taken = _cli_claim(cfg, task, taker, "feat/taken", "--takeover")
    assert taken.returncode == 0, taken.stderr
    assert _claimed_worktree(taken) == path
    new_holder = work.active_claim_for_task(conn, task)
    assert new_holder is not None and new_holder["agent"] == taker


def test_reclaim_after_release_keeping_worktree(committed_conn, cfg, tmp_path):
    conn = committed_conn
    repo, (owner,) = _repo_and_agents(conn, tmp_path, "owner")
    task = work.create_task(conn, repo, "paused work")
    conn.commit()
    claimed = _cli_claim(cfg, task, owner, "feat/paused")
    assert claimed.returncode == 0, claimed.stderr
    path = _claimed_worktree(claimed)
    first_claim = work.active_claim_for_task(conn, task)
    assert first_claim is not None
    released = _cli(cfg, "release", str(first_claim["id"]), agent=owner)
    assert released.returncode == 0, released.stderr

    again = _cli_claim(cfg, task, owner, "feat/paused")
    assert again.returncode == 0, again.stderr
    assert _claimed_worktree(again) == path


def test_other_tasks_retained_worktree_is_not_adopted(committed_conn, cfg, tmp_path):
    conn = committed_conn
    repo, (a, b) = _repo_and_agents(conn, tmp_path, "a", "b")
    task_one = work.create_task(conn, repo, "one")
    task_two = work.create_task(conn, repo, "two")
    conn.commit()
    claimed = _cli_claim(cfg, task_one, a, "feat/shared")
    assert claimed.returncode == 0, claimed.stderr
    _expire(conn, task_one)
    conn.commit()

    refused = _cli_claim(cfg, task_two, b, "feat/shared")
    assert refused.returncode == 1
    assert "belongs to another task" in refused.stderr
    assert "already checked out" not in refused.stderr
    assert work.active_claim_for_task(conn, task_two) is None


def test_drifted_retained_worktree_is_not_adopted(committed_conn, cfg, tmp_path):
    conn = committed_conn
    repo, (a, b) = _repo_and_agents(conn, tmp_path, "a", "b")
    task = work.create_task(conn, repo, "drifting")
    conn.commit()
    claimed = _cli_claim(cfg, task, a, "feat/drift")
    assert claimed.returncode == 0, claimed.stderr
    path = _claimed_worktree(claimed)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-q", "-b", "elsewhere"],
        check=True,
        capture_output=True,
    )
    _expire(conn, task)
    conn.commit()

    refused = _cli_claim(cfg, task, b, "feat/drift")
    assert refused.returncode == 1
    assert "branch_mismatch" in refused.stderr
    expired = conn.execute(
        "SELECT count(*) FROM claims WHERE task_id = ? AND released_at IS NULL",
        (task,),
    ).fetchone()[0]
    assert expired == 1, "the refused claim must roll back entirely"


def test_managed_paths_never_contain_names(conn, cfg, tmp_path):
    hostile = "../../../escaped"
    repo = f"../../outside-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo, make_git_repo(tmp_path / "named"))
    ensure_agent(conn, hostile)
    task = work.create_task(conn, repo, "hostile names")
    claim, _, _ = work.claim_task(conn, task, hostile, "feat/names", ["src/**"])

    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    path = Path(wt["path"])
    assert path.is_relative_to(cfg.worktrees_dir.resolve())
    assert "escaped" not in wt["path"] and "outside" not in wt["path"]
    assert path.is_dir()
