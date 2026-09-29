"""Declared scopes are canonical at claim time and enforced at push time.

Ported from PR #10 (review findings 3 and 4):

3. Scope globs are normalized before they are stored and compared, and scopes
   that escape the repository are refused.
4. Pushes to a claimed branch are held to the claim's declared scopes.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import registry, work
from tests.conftest import make_git_repo
from tests.test_gate import _commit, _push, _setup

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def _local_setup(conn, tmp_path, agents=("a", "b")):
    """A registered working repository (local model) plus named agents."""
    suffix = uuid.uuid4().hex[:8]
    repo_path = make_git_repo(tmp_path / f"repo-{suffix}")
    repo = f"local-{suffix}"
    registry.add_repository(conn, repo, repo_path)
    names = [f"{agent}-{suffix}" for agent in agents]
    for name in names:
        registry.add_agent(conn, name)
    task = work.create_task(conn, repo, "work")
    conn.commit()
    return repo, task, names


# ------------------------------------------------------ claim-time scopes


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("./src/**", "src/**"),
        ("src//api/**", "src/api/**"),
        ("/src/**", "src/**"),
        ("src\\api\\**", "src/api/**"),
        (".", "**"),
        ("src/../lib/**", "lib/**"),
    ],
)
def test_normalize_scope(raw, normalized):
    assert work.normalize_scope(raw) == normalized


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("./src/**", "src/**"),
        ("src\\api\\**", "src/api/handlers.py"),
        ("Src/**", "src/app.py"),
        ("src/../lib/**", "lib/x.py"),
    ],
)
def test_differently_spelled_scopes_overlap(a, b):
    assert work.scopes_overlap(a, b)
    assert work.scopes_overlap(b, a)


def test_scopes_escaping_the_repository_are_refused(conn, tmp_path):
    _repo, task, (a, _b) = _local_setup(conn, tmp_path)
    with pytest.raises(work.ClaimRefused, match="escapes the repository"):
        work.claim_task(conn, task, a, "feat/escape", ["../other/**"])


def test_dot_slash_scope_is_classified_overlapping(conn, tmp_path):
    repo, task, (a, b) = _local_setup(conn, tmp_path)
    work.claim_task(conn, task, a, "feat/one", ["src/**"])
    other = work.create_task(conn, repo, "second")
    with pytest.raises(work.ClaimRefused, match="overlap"):
        work.claim_task(conn, other, b, "feat/two", ["./src/**"])

    claim_id, classification, _ = work.claim_task(
        conn, other, b, "feat/two", ["./src/**"], override_overlap=True
    )
    assert classification == "OVERLAPPING"
    assert work.claim_scopes(conn, claim_id) == ["src/**"]


# -------------------------------------------------------- push-time scopes


@pytest.mark.parametrize(
    ("scope", "path", "inside"),
    [
        ("src/**", "src/a/b.py", True),
        ("src/**", "srcx/a.py", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("docs", "docs/guide.md", True),
        ("docs", "docs", True),
        ("docs", "docs2/x", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "a/b/c.md", True),
        ("src/[ab].py", "src/a.py", True),
        ("src/[!ab].py", "src/a.py", False),
        ("**", "anything/at/all", True),
    ],
)
def test_path_in_scopes(scope, path, inside):
    assert work.path_in_scopes(path, [scope]) is inside


def test_hook_holds_claimed_branch_to_its_scopes(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "scoped work")
    work.claim_task(conn, task, a, branch="feat/scoped", scope_globs=["src/**"])
    conn.commit()

    _commit(clone, "src/inside.py", branch="feat/scoped")
    inside = _push(clone, a, "feat/scoped", cfg=cfg)
    assert inside.returncode == 0, inside.stderr

    _commit(clone, "README.md", branch="feat/scoped")
    outside = _push(clone, a, "feat/scoped", cfg=cfg)
    assert outside.returncode != 0
    assert "outside claim" in outside.stderr
    assert "README.md" in outside.stderr

    # Undo the out-of-scope commit; merging another branch's accepted work in
    # does not attribute that branch's files to the claim holder.
    _git(clone, "reset", "--hard", "HEAD~1")
    _commit(clone, "docs/elsewhere.md", branch="feat/base")
    base = _push(clone, b, "feat/base", cfg=cfg)
    assert base.returncode == 0, base.stderr
    _git(clone, "checkout", "feat/scoped")
    _git(clone, "merge", "--no-edit", "feat/base")
    merged = _push(clone, a, "feat/scoped", cfg=cfg)
    assert merged.returncode == 0, merged.stderr


def test_hook_scope_check_covers_new_claimed_branches(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "new branch")
    work.claim_task(conn, task, a, branch="feat/fresh", scope_globs=["src/**"])
    conn.commit()

    _commit(clone, "docs/outside.md", branch="feat/fresh")
    rejected = _push(clone, a, "feat/fresh", cfg=cfg)
    assert rejected.returncode != 0
    assert "docs/outside.md" in rejected.stderr
