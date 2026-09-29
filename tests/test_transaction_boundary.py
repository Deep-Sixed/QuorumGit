"""The push authorization boundary and the store's schema contract.

pre-receive validates and records each ref update but spends nothing. The
reference-transaction hook re-validates it and consumes any approval at the
`prepared` stage, while Git holds the ref locks and can still abort, then
records whether Git committed or aborted. verify_contract() checks every
schema object the governance rules depend on, not only table names.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from quorumgit import gate, registry, store, work
from quorumgit.config import Config
from tests.test_gate import _commit, _push, _setup


def _rev(git_args: list[str], ref: str) -> str:
    return subprocess.run(
        ["git", *git_args, "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _approve_main(conn, repo_name: str, hub: Path, clone: Path) -> tuple[dict, dict]:
    op = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": _rev(["--git-dir", str(hub)], "refs/heads/main"),
        "newrev": _rev(["-C", str(clone)], "HEAD"),
    }
    conn.execute("INSERT INTO agents (name) VALUES ('operator') ON CONFLICT DO NOTHING")
    approval = gate.request_approval(conn, op, requested_by="operator")
    gate.vote(conn, approval["id"], "operator", True)
    conn.commit()
    return op, approval


def _install_update_hook(hub: Path, python_body: str) -> Path:
    """An extra `update` hook: runs after pre-receive, before the ref moves."""
    script = hub / "hooks" / "extra_update.py"
    script.write_text(python_body, encoding="utf-8")
    hook = hub / "hooks" / "update"
    hook.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def _ref_update_statuses(conn, refname: str, newrev: str) -> list[str]:
    conn.commit()
    return [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ref_updates WHERE refname = ? AND newrev = ? "
            "ORDER BY id",
            (refname, newrev),
        ).fetchall()
    ]


# ----------------------------------------------------- transaction boundary


def test_approval_survives_an_update_git_refuses_after_pre_receive(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "src/approved.py")
    op, approval = _approve_main(conn, repo_name, hub, clone)

    refusing = _install_update_hook(hub, "import sys\nsys.exit(1)\n")
    refused = _push(clone, a, "main", cfg=cfg)
    assert refused.returncode != 0
    assert _rev(["--git-dir", str(hub)], "refs/heads/main") == op["oldrev"]
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"
    assert _ref_update_statuses(conn, "refs/heads/main", op["newrev"]) == ["validated"]

    refusing.unlink()
    accepted = _push(clone, a, "main", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
    assert _rev(["--git-dir", str(hub)], "refs/heads/main") == op["newrev"]
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"
    assert _ref_update_statuses(conn, "refs/heads/main", op["newrev"]) == [
        "validated",
        "committed",
    ]


def test_ownership_change_after_pre_receive_aborts_the_update(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, hub, clone, a, b = _setup(conn, tmp_path)
    task = work.create_task(conn, repo_name, "contested")
    claim, _, _ = work.claim_task(conn, task, a, branch="feat/race", scope_globs=["src/**"])
    conn.commit()
    _commit(clone, "src/first.py", branch="feat/race")
    first = _push(clone, a, "feat/race", cfg=cfg)
    assert first.returncode == 0, first.stderr
    before = _rev(["--git-dir", str(hub)], "refs/heads/feat/race")

    # Between pre-receive and the ref update, a's claim is released and b
    # claims the branch. pre-receive saw a as owner; the update must not land.
    _install_update_hook(
        hub,
        "from quorumgit import config, store, work\n"
        "cfg = config.load()\n"
        "conn = store.connect(cfg)\n"
        f"work.release_claim(conn, {claim}, agent={a!r})\n"
        f"work.claim_task(conn, {task}, {b!r}, branch='feat/race', scope_globs=['src/**'])\n"
        "conn.commit()\n"
        "conn.close()\n",
    )
    _commit(clone, "src/second.py", branch="feat/race")
    raced = _push(clone, a, "feat/race", cfg=cfg)
    assert raced.returncode != 0
    assert _rev(["--git-dir", str(hub)], "refs/heads/feat/race") == before
    newrev = _rev(["-C", str(clone)], "HEAD")
    assert _ref_update_statuses(conn, "refs/heads/feat/race", newrev) == ["aborted"]
    rejected = conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_type = 'gate.update_prepared' "
        "AND json_extract(detail, '$.newrev') = ?",
        (newrev,),
    ).fetchone()
    assert rejected is not None and rejected[0] == 0


def test_aborted_transaction_restores_the_consumed_approval(
    committed_conn, tmp_path, cfg, monkeypatch
):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "src/restore.py")
    # Make the new commit's objects available in the hub without moving main.
    staged = _push(clone, a, "HEAD:refs/heads/staging", cfg=cfg)
    assert staged.returncode == 0, staged.stderr
    op, approval = _approve_main(conn, repo_name, hub, clone)

    # receive-pack runs hooks from inside the repository with GIT_DIR=".".
    monkeypatch.chdir(hub)
    monkeypatch.setenv("GIT_DIR", ".")
    monkeypatch.setenv("QUORUMGIT_AGENT", a)
    line = [f"{op['oldrev']} {op['newrev']} refs/heads/main\n"]
    assert gate.run_pre_receive(conn, repo_name, line) == 0
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"

    assert gate.run_reference_transaction(conn, repo_name, "prepared", line) == 0
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "consumed"

    assert gate.run_reference_transaction(conn, repo_name, "aborted", line) == 0
    assert gate.get_approval_by_id(conn, approval["id"])["status"] == "approved"
    assert _ref_update_statuses(conn, "refs/heads/main", op["newrev"]) == ["aborted"]
    restored = conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_type = 'approval.restored' "
        "AND entity_id = ?",
        (approval["id"],),
    ).fetchone()
    assert restored is not None and restored[0] == 1


def test_prepared_rejects_an_update_pre_receive_did_not_validate(
    committed_conn, tmp_path, monkeypatch
):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _commit(clone, "docs/one.md", branch="feat/swap")
    _commit(clone, "docs/two.md", branch="feat/swap")
    validated = _rev(["-C", str(clone)], "HEAD")
    swapped = _rev(["-C", str(clone)], "HEAD~1")

    monkeypatch.chdir(hub)
    monkeypatch.setenv("GIT_DIR", ".")
    monkeypatch.setenv("QUORUMGIT_AGENT", a)
    zero = "0" * 40
    conn.execute(
        "INSERT INTO ref_updates (repository_id, refname, oldrev, newrev, "
        "pusher_agent_id) VALUES (?, 'refs/heads/feat/swap', ?, ?, ?)",
        (
            registry.get_repository(conn, repo_name)["id"],
            zero,
            validated,
            registry.get_agent(conn, a)["id"],
        ),
    )
    conn.commit()
    line = [f"{zero} {swapped} refs/heads/feat/swap\n"]
    assert gate.run_reference_transaction(conn, repo_name, "prepared", line) == 1


def test_push_requires_the_reference_transaction_hook(committed_conn, tmp_path, cfg):
    conn = committed_conn
    _repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    (hub / "hooks" / "reference-transaction").unlink()
    _commit(clone, "docs/free.md", branch="feat/nohook")
    rejected = _push(clone, a, "feat/nohook", cfg=cfg)
    assert rejected.returncode != 0
    assert "reference-transaction hook is missing" in rejected.stderr


def test_local_ref_maintenance_in_the_hub_passes_through(
    committed_conn, tmp_path, cfg
):
    _repo_name, hub, _clone, _a, _b = _setup(committed_conn, tmp_path)
    head = _rev(["--git-dir", str(hub)], "refs/heads/main")
    for data_dir in (cfg.data_dir, tmp_path / "no-store-here"):
        env = {**os.environ, "QUORUMGIT_DATA_DIR": str(data_dir)}
        env.pop("QUORUMGIT_AGENT", None)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["git", "--git-dir", str(hub), "update-ref",
             f"refs/heads/local-{data_dir.name}", head],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_install_refuses_foreign_reference_transaction_hook_before_writing(
    conn, tmp_path
):
    from tests.test_hook_integrity import _hub_with_clone

    hub, _clone = _hub_with_clone(tmp_path, "foreign-rt")
    repo_name = f"foreign-rt-{tmp_path.name[-8:]}"
    registry.add_repository(conn, repo_name, hub)
    foreign = hub / "hooks" / "reference-transaction"
    foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    foreign.chmod(0o755)

    with pytest.raises(gate.GateError, match="not owned by QuorumGit"):
        gate.install_hook(conn, repo_name)
    assert not (hub / "hooks" / "pre-receive").exists()
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_install_writes_both_hooks(conn, tmp_path):
    from tests.test_hook_integrity import _hub_with_clone

    hub, _clone = _hub_with_clone(tmp_path, "both-hooks")
    repo_name = f"both-{tmp_path.name[-8:]}"
    registry.add_repository(conn, repo_name, hub)
    gate.install_hook(conn, repo_name)
    rt = (hub / "hooks" / "reference-transaction").read_text(encoding="utf-8")
    assert rt == gate._reference_transaction_script(repo_name)
    assert gate.REFERENCE_TRANSACTION_MARKER in rt


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


@pytest.mark.parametrize(
    "trigger",
    ["audit_events_no_delete", "audit_events_no_update", "votes_require_registered_voter"],
)
def test_contract_rejects_a_missing_governance_trigger(fresh_store, trigger):
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
    _tamper(fresh_store, "INSERT INTO schema_migrations (version) VALUES ('999_future.sql')")
    with pytest.raises(store.ContractViolation, match="newer version"):
        store.verify_contract(fresh_store)


def test_contract_tolerates_formatting_only_schema_differences(fresh_store):
    _tamper(
        fresh_store,
        "DROP INDEX ref_updates_in_flight",
        "CREATE INDEX ref_updates_in_flight ON ref_updates ( repository_id,refname , "
        "newrev )   WHERE status IN ('validated','prepared')",
    )
    store.verify_contract(fresh_store)


def test_identity_lookup_ignores_the_hooks_git_dir(tmp_path, monkeypatch):
    from tests.conftest import make_git_repo

    hub = make_git_repo(tmp_path / "hook-repo")
    other = make_git_repo(tmp_path / "other-repo")
    monkeypatch.setenv("GIT_DIR", str(hub / ".git"))
    assert registry.git_common_dir(other) == (other / ".git").resolve()
