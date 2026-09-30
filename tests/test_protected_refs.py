"""Protected refs are stored and matched as full ref names."""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import audit, cli, gate, registry, store
from quorumgit.config import Config
from tests.conftest import OPERATOR, approve, ensure_agent, make_git_repo
from tests.test_gate import _commit, _push


def _hub(conn, tmp_path: Path, protected_refs: list[str]):
    seed = make_git_repo(tmp_path / "seed")
    hub = tmp_path / "hub.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(hub)],
        check=True,
        capture_output=True,
    )
    name = f"hub-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, name, hub, protected_refs=protected_refs)
    pusher = f"pusher-{uuid.uuid4().hex[:8]}"
    registry.add_agent(conn, pusher)
    gate.install_hook(conn, name)
    conn.commit()
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(hub), str(clone)], check=True, capture_output=True
    )
    return name, clone, pusher


def test_short_protected_ref_name_protects_the_branch(committed_conn, tmp_path, cfg):
    """`--protected-ref main` used to be stored as typed and protect nothing."""
    conn = committed_conn
    name, clone, pusher = _hub(conn, tmp_path, ["main"])
    assert registry.get_repository(conn, name)["protected_refs"] == ["refs/heads/main"]

    _commit(clone, "change.txt")
    rejected = _push(clone, pusher, "HEAD:refs/heads/main", cfg=cfg)
    assert rejected.returncode != 0
    assert "protected_ref_update on refs/heads/main" in rejected.stderr

    newrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    oldrev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    approve(conn, {
        "type": "protected_ref_update",
        "repository": name,
        "refname": "refs/heads/main",
        "oldrev": oldrev,
        "newrev": newrev,
    }, requested_by=pusher)
    conn.commit()
    accepted = _push(clone, pusher, "HEAD:refs/heads/main", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("main", "refs/heads/main"),
        (" release/1.0 ", "refs/heads/release/1.0"),
        ("refs/heads/main", "refs/heads/main"),
        ("refs/tags/v1", "refs/tags/v1"),
    ],
)
def test_protected_ref_names_are_normalized(given, stored):
    assert registry.normalize_protected_ref(given) == stored


@pytest.mark.parametrize("bad", ["", "  ", "-main", "HEAD", "bad..name", "a b", "refs/"])
def test_invalid_protected_refs_are_refused(bad):
    with pytest.raises(registry.RegistryError, match="Invalid protected ref"):
        registry.normalize_protected_ref(bad)


def test_protect_ref_command_is_operator_only_and_audited(conn, tmp_path):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "ref-worker")
    name = f"repo-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, name, make_git_repo(tmp_path / "r"))
    repo_id = registry.get_repository(conn, name)["id"]

    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_protected_ref(conn, name, "main", actor="ref-worker")

    assert registry.set_protected_ref(conn, name, "main", actor=OPERATOR) == [
        "refs/heads/main"
    ]
    # The same branch spelled in full is the same protected ref.
    assert registry.set_protected_ref(
        conn, name, "refs/heads/main", actor=OPERATOR
    ) == ["refs/heads/main"]
    assert registry.set_protected_ref(
        conn, name, "main", actor=OPERATOR, remove=True
    ) == []

    events = [
        (e["event_type"], e["detail"]["refname"])
        for e in audit.events(conn, entity="repository", entity_id=repo_id)
        if e["event_type"].startswith("repository.protected_ref")
    ]
    # Newest first; re-adding the same ref was a no-op and left no event.
    assert events == [
        ("repository.protected_ref_removed", "refs/heads/main"),
        ("repository.protected_ref_added", "refs/heads/main"),
    ]


def test_store_refuses_short_protected_ref_names(conn, tmp_path):
    name = f"repo-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, name, make_git_repo(tmp_path / "r"))
    repo_id = registry.get_repository(conn, name)["id"]
    with pytest.raises(sqlite3.IntegrityError, match="full ref names"):
        conn.execute(
            "INSERT INTO protected_refs (repository_id, refname) VALUES (?, 'main')",
            (repo_id,),
        )
    conn.execute(
        "INSERT INTO protected_refs (repository_id, refname) "
        "VALUES (?, 'refs/heads/main')",
        (repo_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="full ref names"):
        conn.execute(
            "UPDATE protected_refs SET refname = 'main' WHERE repository_id = ?",
            (repo_id,),
        )


def test_upgrade_expands_short_protected_ref_names(tmp_path):
    local = Config(data_dir=tmp_path / "upgrade", agent=None)
    local.data_dir.mkdir(parents=True)
    conn = store.open_connection(local)
    try:
        conn.execute(store.SCHEMA_MIGRATIONS_DDL)
        for migration in store._migration_files():
            if migration.name >= "010":
                break
            for statement in store._migration_statements(
                migration.read_text(encoding="utf-8")
            ):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (migration.name,),
            )
        repo_id = conn.execute(
            "INSERT INTO repositories (name, path) VALUES ('old', ?) RETURNING id",
            (str(tmp_path),),
        ).fetchone()[0]
        conn.executemany(
            "INSERT INTO protected_refs (repository_id, refname) VALUES (?, ?)",
            [
                (repo_id, "main"),
                (repo_id, "release"),
                (repo_id, "refs/heads/release"),
                (repo_id, "refs/tags/v1"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    assert store.migrate(local) == [
        "010_protected_ref_names.sql",
        "011_quorum_consumer_separation.sql",
        "012_protected_fields.sql",
    ]
    upgraded = store.connect(local)
    try:
        refs = sorted(
            row[0]
            for row in upgraded.execute(
                "SELECT refname FROM protected_refs WHERE repository_id = ?",
                (repo_id,),
            )
        )
    finally:
        upgraded.close()
    assert refs == ["refs/heads/main", "refs/heads/release", "refs/tags/v1"]
    store.destroy(local)


def test_cli_protect_ref(committed_conn, tmp_path, cfg, monkeypatch, capsys):
    conn = committed_conn
    ensure_agent(conn, OPERATOR, "operator")
    name = f"repo-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, name, make_git_repo(tmp_path / "r"))
    conn.commit()
    monkeypatch.setenv("QUORUMGIT_DATA_DIR", str(cfg.data_dir))
    monkeypatch.delenv("QUORUMGIT_AGENT", raising=False)

    assert cli.main(["repo", "protect-ref", name, "main", "--agent", OPERATOR]) == 0
    assert f"protected refs for {name}: refs/heads/main" in capsys.readouterr().out
    assert cli.main(["repo", "protect-ref", name, "bad..ref", "--agent", OPERATOR]) == 1
    assert "Invalid protected ref" in capsys.readouterr().err
    assert cli.main([
        "repo", "protect-ref", name, "refs/heads/main", "--remove", "--agent", OPERATOR,
    ]) == 0
    assert f"protected refs for {name}: -" in capsys.readouterr().out
