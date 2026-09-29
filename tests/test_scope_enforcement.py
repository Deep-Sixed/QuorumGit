"""Claim scopes are enforced on what a push actually changes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from quorumgit import gate, work
from tests.test_gate import _commit, _push, _register_agents, _setup

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def _claim(conn, repo_name, agent, branch, *scopes):
    task = work.create_task(conn, repo_name, f"work on {branch}")
    work.claim_task(conn, task, agent, branch=branch, scope_globs=list(scopes))
    conn.commit()


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("src/**", "src/app.py", True),
        ("src/**", "src/deep/nested/mod.py", True),
        ("src/**", "srcfoo/app.py", False),
        ("src/**", "docs/guide.md", False),
        ("src/*.py", "src/app.py", True),
        ("src/*.py", "src/pkg/app.py", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "docs/deep/guide.md", True),
        ("src/**/test_*.py", "src/test_x.py", True),
        ("src/**/test_*.py", "src/a/b/test_x.py", True),
        ("src/**/test_*.py", "src/a/b/x.py", False),
        ("docs", "docs/guide.md", True),
        ("docs/", "docs/guide.md", True),
        ("docs", "docs", True),
        ("docs", "docsite/index.md", False),
        ("README.md", "README.md", True),
        ("./src/**", "src/app.py", True),
        ("src/[ab].py", "src/a.py", True),
        ("src/[!ab].py", "src/a.py", False),
        ("src/?.py", "src/a.py", True),
        ("src/?.py", "src/ab.py", False),
        ("**", "anything/at/all", True),
    ],
)
def test_scope_matches(glob, path, expected):
    assert work.scope_matches(glob, path) is expected


def test_paths_outside_scopes_is_sorted_and_unique():
    assert work.paths_outside_scopes(
        ["docs/b.md", "src/a.py", "docs/a.md", "docs/b.md"], ["src/**"]
    ) == ["docs/a.md", "docs/b.md"]


def test_in_scope_push_is_accepted(committed_conn, tmp_path, cfg):
    repo_name, _hub, clone, a, _b = _setup(committed_conn, tmp_path)
    _claim(committed_conn, repo_name, a, "feat/in", "src/**")
    _commit(clone, "src/in_scope.py", branch="feat/in")
    result = _push(clone, a, "feat/in", cfg=cfg)
    assert result.returncode == 0, result.stderr


def test_out_of_scope_push_requires_exact_approval(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _claim(conn, repo_name, a, "feat/out", "src/**")
    _commit(clone, "src/ok.py", branch="feat/out")
    _commit(clone, "docs/stray.md")

    rejected = _push(clone, a, "feat/out", cfg=cfg)
    assert rejected.returncode != 0
    assert "out_of_scope_update" in rejected.stderr
    assert "docs/stray.md" in rejected.stderr
    assert "src/ok.py" not in rejected.stderr

    op = {
        "type": "out_of_scope_update",
        "repository": repo_name,
        "refname": "refs/heads/feat/out",
        "oldrev": "0" * 40,
        "newrev": _git(clone, "rev-parse", "HEAD"),
        "out_of_scope_paths": ["docs/stray.md"],
    }
    assert gate.operation_hash(op) in rejected.stderr
    _register_agents(conn, "operator")
    approval = gate.request_approval(conn, op, requested_by="operator")
    gate.vote(conn, approval["id"], "operator", True)
    conn.commit()

    accepted = _push(clone, a, "feat/out", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"
    hub_tip = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/feat/out"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert hub_tip == op["newrev"]


def test_follow_up_push_is_checked_against_its_own_commits(
    committed_conn, tmp_path, cfg
):
    repo_name, _hub, clone, a, _b = _setup(committed_conn, tmp_path)
    _claim(committed_conn, repo_name, a, "feat/next", "src/**")
    _commit(clone, "src/first.py", branch="feat/next")
    assert _push(clone, a, "feat/next", cfg=cfg).returncode == 0

    _commit(clone, "src/second.py")
    assert _push(clone, a, "feat/next", cfg=cfg).returncode == 0

    _commit(clone, "README.md")
    stray = _push(clone, a, "feat/next", cfg=cfg)
    assert stray.returncode != 0
    assert "README.md" in stray.stderr


def test_rename_out_of_scope_is_detected(committed_conn, tmp_path, cfg):
    """Moving a file into scope still deletes it from outside the scope."""
    repo_name, _hub, clone, a, _b = _setup(committed_conn, tmp_path)
    _claim(committed_conn, repo_name, a, "feat/move", "src/**")
    _git(clone, "checkout", "-B", "feat/move")
    _git(clone, "mv", "docs/guide.md", "src/guide.md")
    _git(clone, "commit", "-m", "move guide into src")

    result = _push(clone, a, "feat/move", cfg=cfg)
    assert result.returncode != 0
    assert "docs/guide.md" in result.stderr


def test_merging_published_history_is_not_charged(committed_conn, tmp_path, cfg):
    """Bringing in main's already-pushed changes is not an out-of-scope edit."""
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    _claim(conn, repo_name, a, "feat/merge", "src/**")
    _commit(clone, "src/feature.py", branch="feat/merge")
    assert _push(clone, a, "feat/merge", cfg=cfg).returncode == 0

    # Another agent publishes docs work on an unclaimed branch.
    _git(clone, "checkout", "-B", "docs-work", "main")
    _commit(clone, "docs/published.md")
    assert _push(clone, b, "docs-work", cfg=cfg).returncode == 0

    _git(clone, "checkout", "feat/merge")
    _git(clone, "merge", "--no-edit", "docs-work")
    result = _push(clone, a, "feat/merge", cfg=cfg)
    assert result.returncode == 0, result.stderr


def test_edits_hidden_in_a_merge_commit_are_charged(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    _claim(conn, repo_name, a, "feat/evil", "src/**")
    _commit(clone, "src/feature.py", branch="feat/evil")
    assert _push(clone, a, "feat/evil", cfg=cfg).returncode == 0

    _git(clone, "checkout", "-B", "docs-work", "main")
    _commit(clone, "docs/published.md")
    assert _push(clone, b, "docs-work", cfg=cfg).returncode == 0

    _git(clone, "checkout", "feat/evil")
    _git(clone, "merge", "--no-commit", "--no-ff", "docs-work")
    (clone / "docs" / "sneaky.md").write_text("added inside the merge\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--no-edit")
    result = _push(clone, a, "feat/evil", cfg=cfg)
    assert result.returncode != 0
    assert "docs/sneaky.md" in result.stderr
    assert "docs/published.md" not in result.stderr


def test_unclaimed_branch_is_not_scope_checked(committed_conn, tmp_path, cfg):
    _repo_name, _hub, clone, _a, b = _setup(committed_conn, tmp_path)
    _commit(clone, "docs/anything.md", branch="free")
    result = _push(clone, b, "free", cfg=cfg)
    assert result.returncode == 0, result.stderr
