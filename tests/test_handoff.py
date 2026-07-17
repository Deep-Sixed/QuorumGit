"""Phase 4 + the handoff-reservation boundary.

An open handoff must reserve the task and branch during the HANDOFF AVAILABLE
interval, so the clean state model is ACTIVE CLAIM -> HANDOFF AVAILABLE ->
SUCCESSOR CLAIM, never briefly UNCLAIMED.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, handoff, registry, work
from tests.conftest import make_git_repo


def _setup(conn, git_repo):
    suffix = uuid.uuid4().hex[:8]
    repo = f"repo-{suffix}"
    registry.add_repository(conn, repo, git_repo)
    a, b, c = f"a-{suffix}", f"b-{suffix}", f"c-{suffix}"
    for name in (a, b, c):
        registry.add_agent(conn, name)
    return repo, a, b, c


def _head(repo_path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _handoff_record(repo_path):
    # last_commit must be a real commit in the registered repository.
    return {"completed": "part", "remaining": "rest",
            "last_commit": _head(repo_path)}


def test_open_handoff_reserves_task(conn, git_repo):
    repo, a, b, c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "reserved work")
    claim_a, _, _ = work.claim_task(conn, task, a, branch="feat/x",
                                    scope_globs=["src/**"])
    handoff.create_handoff(conn, claim_a, a, _handoff_record(git_repo),
                           to_agent=b)

    # The task is NOT ordinary unclaimed work during the gap.
    assert work.get_task(conn, task)["status"] == "handoff"

    # A third agent cannot claim the reserved task through the normal path.
    with pytest.raises(work.ClaimRefused, match="reserved for open handoff"):
        work.claim_task(conn, task, c, branch="feat/other",
                        scope_globs=["src/**"])

    # The reservation is recorded as a conflict event.
    blocked = conn.execute(
        "SELECT count(*) FROM conflict_events WHERE task_id = %s "
        "AND classification = 'BLOCKED'", (task,)
    ).fetchone()[0]
    assert blocked >= 1


def test_decline_returns_task_to_claimable(conn, git_repo):
    repo, a, b, c = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "declined work")
    claim_a, _, _ = work.claim_task(conn, task, a, branch="feat/x",
                                    scope_globs=["src/**"])
    hid = handoff.create_handoff(conn, claim_a, a, _handoff_record(git_repo),
                                 to_agent=b)

    handoff.decline_handoff(conn, hid, b)
    assert work.get_task(conn, task)["status"] == "open"

    # After decline the task is claimable again by anyone.
    new_claim, classification, _ = work.claim_task(
        conn, task, c, branch="feat/fresh", scope_globs=["src/**"]
    )
    assert new_claim and classification in ("CLEAR", "RELATED")


def test_accept_bypasses_own_reservation(conn, git_repo):
    repo, a, b, _ = _setup(conn, git_repo)
    task = work.create_task(conn, repo, "handed work")
    claim_a, _, _ = work.claim_task(conn, task, a, branch="feat/x",
                                    scope_globs=["src/**"])
    hid = handoff.create_handoff(conn, claim_a, a, _handoff_record(git_repo),
                                 to_agent=b)
    result = handoff.accept_handoff(conn, hid, b)
    assert result["claim_id"]
    assert work.get_task(conn, task)["status"] == "claimed"


def test_branch_frozen_during_handoff_gap(committed_conn, tmp_path, cfg):
    """The former owner cannot push to the branch during the gap."""
    conn = committed_conn
    repo, a, b, _ = _setup(conn, git_repo=make_git_repo(tmp_path / "seed"))
    # rebuild as a bare hub with hook + clone
    seed = tmp_path / "seed"
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(hub)],
                   check=True, capture_output=True)
    # re-register the hub under the same repo name space
    repo2 = f"hub-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo2, hub)
    gate.install_hook(conn, repo2)
    task = work.create_task(conn, repo2, "frozen branch work")
    claim_a, _, _ = work.claim_task(conn, task, a, branch="feat/freeze",
                                    scope_globs=["src/**"])
    handoff.create_handoff(conn, claim_a, a, _handoff_record(hub), to_agent=b)
    conn.commit()

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(hub), str(clone)],
                   check=True, capture_output=True)
    env_base = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
    }

    def git(*args, **kw):
        import os
        return subprocess.run(["git", "-C", str(clone), *args],
                              capture_output=True, text=True,
                              env={**os.environ, **env_base, **kw})

    git("checkout", "-B", "feat/freeze")
    (clone / "src").mkdir(exist_ok=True)
    (clone / "src" / "z.py").write_text("x\n")
    git("add", "-A")
    git("commit", "-m", "edit")
    import os
    result = subprocess.run(
        ["git", "-C", str(clone), "push", "origin", "feat/freeze"],
        capture_output=True, text=True,
        env={**os.environ, **env_base, "QUORUMGIT_AGENT": a,
             "QUORUMGIT_DATA_DIR": str(cfg.data_dir),
             "QUORUMGIT_PG_INSTANCE": cfg.pg_instance,
             "QUORUMGIT_PG_PORT": str(cfg.pg_port),
             "PATH": str(Path(__import__("sys").executable).parent)
                     + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode != 0
    assert "frozen pending handoff" in result.stderr
