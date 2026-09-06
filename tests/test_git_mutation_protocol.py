"""Regression coverage for the receive reservation/completion protocol."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, receive, registry, store, work
from quorumgit.config import Config
from tests.conftest import make_git_repo
from tests.test_gate import _commit, _push


def _setup(committed_conn, tmp_path, cfg):
    seed = make_git_repo(tmp_path / "seed")
    hub = tmp_path / "hub.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(hub)],
        check=True,
        capture_output=True,
    )
    repo_name = f"mutation-{uuid.uuid4().hex[:8]}"
    owner = f"owner-{uuid.uuid4().hex[:8]}"
    operator = f"operator-{uuid.uuid4().hex[:8]}"
    registry.add_repository(
        committed_conn,
        repo_name,
        hub,
        protected_refs=["refs/heads/main"],
    )
    registry.add_agent(committed_conn, owner)
    registry.add_agent(committed_conn, operator)
    receive.install_hooks(committed_conn, repo_name)
    committed_conn.commit()

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(hub), str(clone)],
        check=True,
        capture_output=True,
    )
    return repo_name, hub, clone, owner, operator


def _approve_main_update(conn, repo_name, hub, clone, operator):
    oldrev = subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    newrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    operation = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": oldrev,
        "newrev": newrev,
    }
    approval = gate.request_approval(conn, operation, requested_by=operator)
    gate.vote(conn, approval["id"], operator, True)
    conn.commit()
    return approval, operation


def test_successful_push_completes_reservation_and_consumes_approval(
    committed_conn, tmp_path, cfg
):
    repo_name, hub, clone, owner, operator = _setup(committed_conn, tmp_path, cfg)
    _commit(clone, "reserved-success.txt")
    approval, operation = _approve_main_update(
        committed_conn, repo_name, hub, clone, operator
    )

    result = _push(clone, owner, "main", cfg=cfg)
    assert result.returncode == 0, result.stderr

    completed = committed_conn.execute(
        """
        SELECT status, approval_id, completed_at
        FROM git_mutations
        WHERE repository_id = (SELECT id FROM repositories WHERE name = ?)
          AND refname = 'refs/heads/main'
        ORDER BY id DESC LIMIT 1
        """,
        (repo_name,),
    ).fetchone()
    assert completed is not None
    assert completed[0] == "completed"
    assert completed[1] == approval["id"]
    assert completed[2] is not None
    assert gate.get_approval_by_id(committed_conn, approval["id"])["status"] == (
        "consumed"
    )
    assert subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == operation["newrev"]


def test_rejected_update_keeps_approval_and_retry_reuses_reservation(
    committed_conn, tmp_path, cfg
):
    repo_name, hub, clone, owner, operator = _setup(committed_conn, tmp_path, cfg)
    _commit(clone, "reserved-retry.txt")
    approval, operation = _approve_main_update(
        committed_conn, repo_name, hub, clone, operator
    )

    update_hook = hub / "hooks" / "update"
    update_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    update_hook.chmod(0o755)
    rejected = _push(clone, owner, "main", cfg=cfg)
    assert rejected.returncode != 0
    assert gate.get_approval_by_id(committed_conn, approval["id"])["status"] == (
        "approved"
    )
    reservation = committed_conn.execute(
        """
        SELECT id, status, approval_id FROM git_mutations
        WHERE repository_id = (SELECT id FROM repositories WHERE name = ?)
          AND refname = 'refs/heads/main' AND status = 'reserved'
        """,
        (repo_name,),
    ).fetchone()
    assert reservation is not None and reservation[2] == approval["id"]
    assert subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == operation["oldrev"]

    update_hook.unlink()
    retried = _push(clone, owner, "main", cfg=cfg)
    assert retried.returncode == 0, retried.stderr
    rows = committed_conn.execute(
        """
        SELECT id, status FROM git_mutations
        WHERE repository_id = (SELECT id FROM repositories WHERE name = ?)
          AND refname = 'refs/heads/main'
        ORDER BY id
        """,
        (repo_name,),
    ).fetchall()
    assert rows == [(reservation[0], "completed")]
    assert gate.get_approval_by_id(committed_conn, approval["id"])["status"] == (
        "consumed"
    )


def test_inflight_branch_freezes_claim_transition(tmp_path, monkeypatch):
    """The reservation freezes ownership in an isolated authority store."""
    local_cfg = Config(data_dir=tmp_path / "mutation-data", agent=None)
    store.migrate(local_cfg)
    store.verify_contract(local_cfg)
    conn = store.connect(local_cfg)
    try:
        repo_name, hub, clone, owner, _operator = _setup(conn, tmp_path, local_cfg)
        task = work.create_task(conn, repo_name, "reserved branch")
        claim_id, _, _ = work.claim_task(
            conn,
            task,
            owner,
            branch="feat/reserved",
            scope_globs=["src/**"],
        )
        conn.commit()

        _commit(clone, "src/reserved.py", branch="feat/reserved")
        newrev = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        zero = "0" * 40
        line = f"{zero} {newrev} refs/heads/feat/reserved\n"
        monkeypatch.setenv("GIT_DIR", str(hub))
        monkeypatch.setenv("QUORUMGIT_AGENT", owner)
        assert receive.run_pre_receive(conn, repo_name, [line]) == 0

        blocked = store.connect(local_cfg)
        try:
            with pytest.raises(ValueError, match="in-flight Git mutation"):
                work.release_claim(blocked, claim_id, owner)
            blocked.rollback()
        finally:
            blocked.close()

        subprocess.run(
            [
                "git",
                "--git-dir",
                str(hub),
                "update-ref",
                "refs/heads/feat/reserved",
                newrev,
            ],
            check=True,
            capture_output=True,
        )
        assert receive.run_post_receive(conn, repo_name, [line]) == 0

        after = store.connect(local_cfg)
        try:
            work.release_claim(after, claim_id, owner)
            after.commit()
        finally:
            after.close()
    finally:
        conn.close()
        store.destroy(local_cfg)


def test_install_refuses_unrelated_post_hook_without_changing_it(conn, tmp_path):
    seed = make_git_repo(tmp_path / "post-seed")
    hub = tmp_path / "post-hub.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(hub)],
        check=True,
        capture_output=True,
    )
    repo_name = f"post-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo_name, hub)
    post = hub / "hooks" / "post-receive"
    original = "#!/bin/sh\necho existing-post\n"
    post.write_text(original, encoding="utf-8")
    post.chmod(0o755)

    with pytest.raises(receive.ReceiveError, match="not owned by QuorumGit"):
        receive.install_hooks(conn, repo_name)
    assert post.read_text(encoding="utf-8") == original
    assert not (hub / "hooks" / "pre-receive").exists()
