"""Regression coverage for pre-receive hook integrity and repository binding."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from quorumgit import gate, registry
from quorumgit.registry import RegistryError
from tests.conftest import make_git_repo
from tests.test_gate import _commit, _push


def _hub_with_clone(tmp_path: Path, label: str) -> tuple[Path, Path]:
    seed = make_git_repo(tmp_path / f"{label}-seed")
    hub = tmp_path / f"{label}.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(hub)],
        check=True,
        capture_output=True,
    )
    clone = tmp_path / f"{label}-clone"
    subprocess.run(
        ["git", "clone", str(hub), str(clone)],
        check=True,
        capture_output=True,
    )
    return hub, clone


def test_repository_registration_rejects_same_git_common_dir(conn, git_repo):
    registry.add_repository(conn, f"root-{uuid.uuid4().hex[:8]}", git_repo)
    duplicate_name = f"subdir-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RegistryError, match="already registered"):
        registry.add_repository(conn, duplicate_name, git_repo / "src")


def test_install_refuses_to_overwrite_unrelated_hook(conn, tmp_path):
    hub, _clone = _hub_with_clone(tmp_path, "preserve-hook")
    repo_name = f"preserve-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo_name, hub)

    hook_path = hub / "hooks" / "pre-receive"
    original = "#!/bin/sh\necho unrelated >&2\nexit 1\n"
    hook_path.write_text(original, encoding="utf-8")
    hook_path.chmod(0o755)

    with pytest.raises(gate.GateError, match="not owned by QuorumGit"):
        gate.install_hook(conn, repo_name)
    assert hook_path.read_text(encoding="utf-8") == original


def test_install_honors_effective_core_hookspath(committed_conn, tmp_path, cfg):
    hub, clone = _hub_with_clone(tmp_path, "custom-hooks")
    subprocess.run(
        ["git", "-C", str(hub), "config", "core.hooksPath", "custom-hooks"],
        check=True,
        capture_output=True,
    )
    repo_name = f"custom-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    registry.add_repository(
        committed_conn,
        repo_name,
        hub,
        protected_refs=["refs/heads/main"],
    )
    registry.add_agent(committed_conn, agent)
    hook_path = gate.install_hook(committed_conn, repo_name)
    committed_conn.commit()

    assert hook_path == (hub / "custom-hooks" / "pre-receive").resolve()
    assert hook_path.exists()
    _commit(clone, "custom-hook.txt")
    rejected = _push(clone, agent, "main", cfg=cfg)
    assert rejected.returncode != 0
    assert "requires an approval" in rejected.stderr


def test_repository_name_is_literal_shell_data(committed_conn, tmp_path, cfg):
    hub, clone = _hub_with_clone(tmp_path, "shell-literal")
    marker = hub / "injection-marker"
    repo_name = "evil-$(printf injected > injection-marker)"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    registry.add_repository(committed_conn, repo_name, hub)
    registry.add_agent(committed_conn, agent)
    hook_path = gate.install_hook(committed_conn, repo_name)
    committed_conn.commit()

    hook_text = hook_path.read_text(encoding="utf-8")
    assert gate.HOOK_MARKER in hook_text
    assert not marker.exists()

    _commit(clone, "literal-name.txt")
    pushed = _push(clone, agent, "main", cfg=cfg)
    assert pushed.returncode == 0, pushed.stderr
    assert not marker.exists(), "repository name executed as shell syntax"


def test_hook_policy_is_bound_to_invoking_repository(committed_conn, tmp_path, cfg):
    hub_a, clone_a = _hub_with_clone(tmp_path, "bound-a")
    hub_b, _clone_b = _hub_with_clone(tmp_path, "bound-b")
    repo_a = f"protected-{uuid.uuid4().hex[:8]}"
    repo_b = f"unprotected-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"

    registry.add_repository(
        committed_conn,
        repo_a,
        hub_a,
        protected_refs=["refs/heads/main"],
    )
    registry.add_repository(committed_conn, repo_b, hub_b)
    registry.add_agent(committed_conn, agent)
    hook_b = gate.install_hook(committed_conn, repo_b)
    committed_conn.commit()

    copied = hub_a / "hooks" / "pre-receive"
    copied.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(hook_b, copied)
    copied.chmod(0o755)

    _commit(clone_a, "wrong-policy.txt")
    rejected = _push(clone_a, agent, "main", cfg=cfg)
    assert rejected.returncode != 0
    assert "Hook repository mismatch" in rejected.stderr


def test_legacy_quorumgit_hook_upgrades_in_place(conn, tmp_path):
    hub, _clone = _hub_with_clone(tmp_path, "legacy-hook")
    repo_name = f"legacy-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo_name, hub)

    hook_path = hub / "hooks" / "pre-receive"
    hook_path.write_text(gate._legacy_hook_script(repo_name), encoding="utf-8")
    hook_path.chmod(0o755)

    installed = gate.install_hook(conn, repo_name)
    assert installed == hook_path.resolve()
    assert installed.read_text(encoding="utf-8") == gate._hook_script(repo_name)


def test_hook_install_is_idempotent(conn, tmp_path):
    hub, _clone = _hub_with_clone(tmp_path, "idempotent-hook")
    repo_name = f"idempotent-{uuid.uuid4().hex[:8]}"
    registry.add_repository(conn, repo_name, hub)

    first = gate.install_hook(conn, repo_name)
    first_content = first.read_text(encoding="utf-8")
    second = gate.install_hook(conn, repo_name)
    assert second == first
    assert second.read_text(encoding="utf-8") == first_content
