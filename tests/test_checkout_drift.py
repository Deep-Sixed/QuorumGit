"""Real Git checkout drift must never become an automatic cleanup target."""

import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import registry, trees, work
from tests.conftest import make_git_repo


def test_git_common_dir_resolves_relative_output(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_git(_path, *args):
        calls.append(args)
        return ".git"

    monkeypatch.setattr(trees, "_git", fake_git)
    assert trees._git_common_dir(root) == (root / ".git").resolve()
    assert calls == [("rev-parse", "--git-common-dir")]


@pytest.mark.parametrize("drift", ["branch", "detached", "repository", "dirty"])
def test_doctor_preserves_changed_checkout(conn, git_repo, cfg, drift):
    name = uuid.uuid4().hex
    registry.add_repository(conn, name, git_repo)
    registry.add_agent(conn, name)
    task = work.create_task(conn, name, "checkout drift")
    claim, _, _ = work.claim_task(conn, task, name, "feat/owned", ["src/**"])
    wt = trees.create_worktree(conn, claim, cfg.worktrees_dir)
    path = Path(wt["path"])
    assert not any(
        item["worktree_id"] == wt["id"] for item in trees.doctor_worktrees(conn)
    )
    if drift == "repository":
        subprocess.run(
            ["git", "-C", str(git_repo), "worktree", "remove", str(path)],
            check=True,
            capture_output=True,
        )
        make_git_repo(path)
    elif drift == "dirty":
        (path / "untracked.txt").write_text("preserve agent work", encoding="utf-8")
    else:
        args = (
            ["checkout", "--detach"]
            if drift == "detached"
            else ["checkout", "-b", "feat/other"]
        )
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True
        )
    work.release_claim(conn, claim, name)
    expected = {
        "repository": "repository_mismatch",
        "detached": "detached_head",
        "branch": "branch_mismatch",
        "dirty": "orphaned",
    }[drift]
    report = next(
        item
        for item in trees.doctor_worktrees(conn)
        if item["worktree_id"] == wt["id"]
    )
    assert report["issue"] == expected
    findings = trees.doctor_worktrees(conn, repair=True)
    finding = next(item for item in findings if item["worktree_id"] == wt["id"])
    assert finding["issue"] == expected
    assert not finding["repaired"]
    assert path.exists()
    if drift == "dirty":
        assert (path / "untracked.txt").read_text(encoding="utf-8") == (
            "preserve agent work"
        )
    recorded = trees.worktree_for_claim(conn, claim)
    assert recorded is not None and recorded["removed_at"] is None
