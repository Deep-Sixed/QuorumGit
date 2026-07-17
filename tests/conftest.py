"""Test fixtures: a throwaway pg0 instance and real temporary git repos.

Nothing is mocked — tests run against a live PostgreSQL and real git.
"""

from __future__ import annotations

import os
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from quorumgit import store
from quorumgit.config import Config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def cfg(tmp_path_factory) -> Iterator[Config]:
    data_dir = tmp_path_factory.mktemp("quorumgit-data")
    instance = f"quorumgit-test-{uuid.uuid4().hex[:8]}"
    cfg = Config(
        data_dir=data_dir,
        pg_instance=instance,
        pg_port=_free_port(),
        agent=None,
    )
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    yield cfg
    store.destroy(cfg)


@pytest.fixture(scope="session")
def initialized_store(cfg) -> str:
    uri = store.ensure_running(cfg)
    store.provision_extensions(uri)
    store.migrate(uri)
    store.verify_contract(uri)
    return uri


@pytest.fixture()
def conn(initialized_store):
    with psycopg.connect(initialized_store) as connection:
        yield connection
        connection.rollback()


@pytest.fixture()
def committed_conn(initialized_store):
    """Connection whose work is committed (for CLI/hook interop tests)."""
    with psycopg.connect(initialized_store) as connection:
        yield connection
        connection.commit()


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
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       capture_output=True, env=env)

    subprocess.run(["git", "init", "-b", "main", str(path)], check=True,
                   capture_output=True)
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
