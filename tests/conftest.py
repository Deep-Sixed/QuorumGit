"""Test fixtures: one throwaway local libSQL store and real temporary git repos.

Nothing is mocked — tests run against the actual libSQL engine and real git.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from quorumgit import store
from quorumgit.config import Config


@pytest.fixture(scope="session")
def cfg(tmp_path_factory) -> Iterator[Config]:
    data_dir = tmp_path_factory.mktemp("quorumgit-data")
    cfg = Config(data_dir=data_dir, agent=None)
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    yield cfg
    store.destroy(cfg)


@pytest.fixture(scope="session")
def initialized_store(cfg) -> Config:
    store.migrate(cfg)
    store.verify_contract(cfg)
    return cfg


@pytest.fixture()
def conn(initialized_store):
    connection = store.connect(initialized_store)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture()
def committed_conn(initialized_store):
    """Connection whose work is committed (for CLI/hook interop tests)."""
    connection = store.connect(initialized_store)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def make_git_repo(path: Path) -> Path:
    """A real git repo with one commit on main."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
    }

    def git(*args):
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            env=env,
        )

    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("test repo\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('hello')\n")
    (path / "docs").mkdir()
    (path / "docs" / "guide.md").write_text("guide\n")
    git("add", "-A")
    git("commit", "-m", "initial commit")
    return path


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    return make_git_repo(tmp_path / "repo")
