"""End-to-end hub deployment: `claim --no-worktree` + governed pushes.

An owner reserves a branch without a managed worktree, works from an
external clone, and pushes to the bare hub. The hook accepts the owner,
rejects a stranger, and `checkpoint --commit` records the pushed commit.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from quorumgit import trees, work
from tests.test_gate import _commit, _push, _setup


def _cli(cfg, *args: str, agent: str | None = None):
    env = {
        **os.environ,
        "QUORUMGIT_DATA_DIR": str(cfg.data_dir),
    }
    env.pop("QUORUMGIT_AGENT", None)
    if agent:
        env["QUORUMGIT_AGENT"] = agent
    return subprocess.run(
        [sys.executable, "-m", "quorumgit", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _rev_parse(git_dir_args: list[str], ref: str) -> str:
    return subprocess.run(
        ["git", *git_dir_args, "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_hub_no_worktree_owner_workflow(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, hub, clone, owner, stranger = _setup(conn, tmp_path)
    task_id = work.create_task(conn, repo_name, "hub-model work")
    conn.commit()

    claimed = _cli(
        cfg,
        "claim",
        str(task_id),
        "--branch",
        "feat/hub",
        "--scope",
        "src/**",
        "--no-worktree",
        agent=owner,
    )
    assert claimed.returncode == 0, claimed.stderr
    assert "no worktree" in claimed.stdout
    match = re.search(r"claim (\d+) acquired", claimed.stdout)
    assert match is not None
    claim_id = int(match.group(1))
    assert trees.worktree_for_claim(conn, claim_id) is None

    _commit(clone, "src/hub_change.py", branch="feat/hub")

    rejected = _push(clone, stranger, "feat/hub", cfg=cfg)
    assert rejected.returncode != 0
    assert "claimed by" in rejected.stderr

    accepted = _push(clone, owner, "feat/hub", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr

    pushed = _rev_parse(["--git-dir", str(hub)], "refs/heads/feat/hub")
    assert pushed == _rev_parse(["-C", str(clone)], "HEAD")

    missing = _cli(cfg, "checkpoint", str(claim_id), agent=owner)
    assert missing.returncode == 1
    assert "--commit" in missing.stderr

    cp = _cli(
        cfg,
        "checkpoint",
        str(claim_id),
        "--commit",
        pushed,
        "--note",
        "hub checkpoint",
        agent=owner,
    )
    assert cp.returncode == 0, cp.stderr
    assert pushed in cp.stdout

    row = conn.execute(
        "SELECT commit_oid FROM checkpoints WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    assert row is not None and row[0] == pushed
