"""Independent hardening carried forward from PR #10.

- verify_contract() compares the whole schema against the bundled migrations.
- Repository identity lookups ignore the hook's repository-selecting Git env.
- Commit specs resolve to full OIDs; user errors never print tracebacks.
- `handoff create --last-commit` wins over the worktree's HEAD.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import handoff, registry, store, trees, work
from quorumgit.config import Config
from tests.conftest import make_git_repo
from tests.test_cli_hub import _cli

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


def _local_setup(conn, tmp_path):
    """A registered working repository (local model), one worker, one task."""
    suffix = uuid.uuid4().hex[:8]
    repo_path = make_git_repo(tmp_path / f"repo-{suffix}")
    repo = f"local-{suffix}"
    registry.add_repository(conn, repo, repo_path)
    agent = f"a-{suffix}"
    registry.add_agent(conn, agent)
    task = work.create_task(conn, repo, "work")
    conn.commit()
    return repo_path, task, agent


def _claim_id(output: str) -> int:
    match = re.search(r"claim (\d+) acquired", output)
    assert match is not None, output
    return int(match.group(1))


def _no_traceback(result) -> None:
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.startswith("[quorumgit] ERROR:"), result.stderr


# ------------------------------------------------------------ schema contract


@pytest.fixture()
def fresh_store(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path / "contract", agent=None)
    store.migrate(cfg)
    store.verify_contract(cfg)
    return cfg


def _tamper(cfg: Config, *statements: str) -> None:
    conn = store.open_connection(cfg)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_contract_rejects_a_missing_trigger_outside_the_named_list(fresh_store):
    trigger = "approval_requester_updates_require_registration"
    assert trigger not in store.REQUIRED_TRIGGERS
    _tamper(fresh_store, f"DROP TRIGGER {trigger}")
    with pytest.raises(store.ContractViolation, match=f"missing trigger {trigger}"):
        store.verify_contract(fresh_store)
    with pytest.raises(store.ContractViolation):
        store.connect(fresh_store)


def test_contract_rejects_a_weakened_unique_index(fresh_store):
    _tamper(
        fresh_store,
        "DROP INDEX claims_one_active_per_task",
        "CREATE INDEX claims_one_active_per_task ON claims(task_id) "
        "WHERE released_at IS NULL",
    )
    with pytest.raises(
        store.ContractViolation, match="altered index claims_one_active_per_task"
    ):
        store.verify_contract(fresh_store)


def test_contract_rejects_an_unexpected_trigger(fresh_store):
    _tamper(
        fresh_store,
        "CREATE TRIGGER auto_approve AFTER INSERT ON approvals BEGIN "
        "UPDATE approvals SET status = 'approved' WHERE id = NEW.id; END",
    )
    with pytest.raises(store.ContractViolation, match="unexpected trigger auto_approve"):
        store.verify_contract(fresh_store)


def test_contract_rejects_a_store_from_a_newer_version(fresh_store):
    _tamper(
        fresh_store, "INSERT INTO schema_migrations (version) VALUES ('999_future.sql')"
    )
    with pytest.raises(store.ContractViolation, match="newer version"):
        store.verify_contract(fresh_store)


def test_contract_tolerates_formatting_only_schema_differences(fresh_store):
    _tamper(
        fresh_store,
        "DROP INDEX approvals_one_live_per_operation",
        "CREATE UNIQUE INDEX approvals_one_live_per_operation ON approvals "
        "( operation_hash )   WHERE status IN ('pending','approved')",
    )
    store.verify_contract(fresh_store)


def test_contract_tolerates_keyword_case_and_operator_spacing(fresh_store):
    _tamper(
        fresh_store,
        "DROP INDEX approvals_one_live_per_operation",
        "create unique index approvals_one_live_per_operation on approvals"
        "(operation_hash) where status in ('pending','approved') -- rebuilt",
    )
    store.verify_contract(fresh_store)


def test_contract_still_rejects_a_changed_literal(fresh_store):
    _tamper(
        fresh_store,
        "DROP INDEX approvals_one_live_per_operation",
        "CREATE UNIQUE INDEX approvals_one_live_per_operation ON approvals"
        "(operation_hash) WHERE status IN ('pending', 'APPROVED')",
    )
    with pytest.raises(
        store.ContractViolation, match="altered index approvals_one_live_per_operation"
    ):
        store.verify_contract(fresh_store)


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("CHECK (role <> '')", "check(role<>'')", True),
        ("x IN ('a', 'b')", "x in ('a','b') /* c */", True),
        ("x = 'Worker'", "x = 'worker'", False),
        ('CREATE TABLE "T"(a)', 'create table "t"(a)', False),
    ],
)
def test_schema_sql_normalization(a, b, same):
    assert (store._normalized_sql(a) == store._normalized_sql(b)) is same


# ------------------------------------------------------ repository identity


def test_identity_lookup_ignores_the_hooks_git_dir(tmp_path, monkeypatch):
    hub = make_git_repo(tmp_path / "hook-repo")
    other = make_git_repo(tmp_path / "other-repo")
    monkeypatch.setenv("GIT_DIR", str(hub / ".git"))
    assert registry.git_common_dir(other) == (other / ".git").resolve()


# ------------------------------------------------- commit specs and errors


def test_checkpoint_accepts_abbreviated_commit(committed_conn, tmp_path, cfg):
    repo_path, task, agent = _local_setup(committed_conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/short",
                   "--scope", "src/**", "--no-worktree", agent=agent)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    full = _git(repo_path, "rev-parse", "main")

    for spec in (full[:9], full.upper(), "main"):
        result = _cli(cfg, "checkpoint", str(claim_id), "--commit", spec, agent=agent)
        assert result.returncode == 0, result.stderr
        assert full in result.stdout

    bogus = _cli(cfg, "checkpoint", str(claim_id), "--commit", "nope", agent=agent)
    _no_traceback(bogus)
    assert "does not exist" in bogus.stderr


def test_duplicate_agent_registration_is_a_clean_error(committed_conn, tmp_path, cfg):
    _repo_path, _task, agent = _local_setup(committed_conn, tmp_path)
    result = _cli(cfg, "agent", "add", agent)
    _no_traceback(result)
    assert "already registered" in result.stderr


def test_duplicate_protected_ref_is_deduplicated(committed_conn, tmp_path):
    repo_path = make_git_repo(tmp_path / "dup-refs")
    name = f"dup-{uuid.uuid4().hex[:8]}"
    registry.add_repository(
        committed_conn,
        name,
        repo_path,
        protected_refs=["refs/heads/main", "refs/heads/main"],
    )
    assert registry.get_repository(committed_conn, name)["protected_refs"] == [
        "refs/heads/main"
    ]


@pytest.mark.parametrize("payload", ["{bad", "[1, 2]"])
def test_invalid_operation_json_is_a_clean_error(cfg, initialized_store, payload):
    result = _cli(cfg, "approve", "hash", payload)
    _no_traceback(result)
    assert "Operation" in result.stderr


# ------------------------------------------------------ handoff last commit


def test_explicit_last_commit_wins_over_worktree_head(committed_conn, tmp_path, cfg):
    conn = committed_conn
    _repo_path, task, agent = _local_setup(conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/hand",
                   "--scope", "src/**", agent=agent)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    wt = trees.active_worktree_for_claim(conn, claim_id)
    assert wt is not None
    wt_path = Path(wt["path"])
    base = _git(wt_path, "rev-parse", "HEAD")
    (wt_path / "src" / "more.py").write_text("x\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-m", "later work")
    assert _git(wt_path, "rev-parse", "HEAD") != base

    created = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", "--last-commit", base[:10], agent=agent)
    assert created.returncode == 0, created.stderr
    assert base in created.stdout
    match = re.search(r"handoff (\d+) created", created.stdout)
    assert match is not None, created.stdout
    assert handoff.get_handoff(conn, int(match.group(1)))["record"]["last_commit"] == base


def test_handoff_without_worktree_requires_last_commit(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_path, task, agent = _local_setup(conn, tmp_path)
    claimed = _cli(cfg, "claim", str(task), "--branch", "feat/gone",
                   "--scope", "src/**", agent=agent)
    assert claimed.returncode == 0, claimed.stderr
    claim_id = _claim_id(claimed.stdout)
    trees.remove_worktree(conn, claim_id, agent)
    conn.commit()

    missing = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", agent=agent)
    assert missing.returncode == 1
    assert "--last-commit" in missing.stderr

    head = _git(repo_path, "rev-parse", "feat/gone")
    created = _cli(cfg, "handoff", "create", str(claim_id), "--completed", "c",
                   "--remaining", "r", "--last-commit", head, agent=agent)
    assert created.returncode == 0, created.stderr
