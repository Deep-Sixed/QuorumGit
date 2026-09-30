"""Governance derived from pushed Git objects: scopes, protected paths, refs.

Every push here is a real `git push` through the installed pre-receive hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from quorumgit import cli, gate, git_objects, registry, work
from tests.conftest import OPERATOR, approve, ensure_agent
from tests.test_gate import _push, _setup

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def _edit(clone: Path, *paths: str, message: str = "edit") -> str:
    for path in paths:
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{message}\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", message)
    return _git(clone, "rev-parse", "HEAD")


def _claimed_branch(conn, repo_name, agent, branch, scopes):
    task = work.create_task(conn, repo_name, f"work on {branch}")
    claim_id, _, _ = work.claim_task(
        conn, task, agent, branch=branch, scope_globs=scopes
    )
    conn.commit()
    return claim_id


def _required(plan: dict) -> dict[str, dict]:
    return {entry["operation"]["type"]: entry for entry in plan["operations"]}


# ------------------------------------------------------------ scope matching


@pytest.mark.parametrize(
    ("path", "glob", "expected"),
    [
        ("src/api/routes.py", "src/api/**", True),
        ("src/database/schema.py", "src/api/**", False),
        ("src/app.py", "src/*.py", True),
        ("src/pkg/app.py", "src/*.py", False),
        ("app.py", "**/*.py", True),
        ("a/b/app.py", "**/*.py", True),
        ("src/api/routes.py", "src/api", True),
        ("src/apiary.py", "src/api", False),
        ("src/api/routes.py", "src/api/", True),
        ("SECURITY.md", "SECURITY.md", True),
        (".github/workflows/ci.yml", ".github/workflows/**", True),
        ("x/y", "x/**/y", True),
        ("x/a/b/y", "x/**/y", True),
        ("v1", "v[0-9]", True),
        ("vx", "v[!0-9]", True),
    ],
)
def test_path_in_scope(path, glob, expected):
    assert work.path_in_scopes(path, [glob]) is expected


# ------------------------------------------------------- changed-path derivation


def test_changed_paths_count_only_content_new_to_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    base = _edit(repo, "README.md", message="base")
    _git(repo, "checkout", "-b", "feat")
    feat = _edit(repo, "src/api/routes.py", message="feature")
    _git(repo, "checkout", "main")
    main = _edit(repo, "infra/prod.tf", message="mainline")
    _git(repo, "checkout", "feat")
    _git(repo, "merge", "--no-edit", "main")
    merged = _git(repo, "rev-parse", "HEAD")
    git_dir = git_objects.absolute_git_dir(repo)

    # Merging already-governed mainline content in introduces nothing new.
    assert git_objects.changed_paths(git_dir, feat, merged, [main, feat]) == []
    # A new branch carries only the commits the repository does not have.
    assert git_objects.changed_paths(
        git_dir, git_objects.ZERO_OID, feat, [base]
    ) == ["src/api/routes.py"]
    # A root commit is compared with the empty tree.
    assert git_objects.changed_paths(
        git_dir, git_objects.ZERO_OID, base, []
    ) == ["README.md"]
    assert git_objects.changed_paths(git_dir, feat, git_objects.ZERO_OID, []) == []


def _paths_one_commit_at_a_time(git_dir: Path, commit: str) -> list[str]:
    """The per-commit derivation paths_by_commit replaces, as an oracle."""
    out = subprocess.run(
        ["git", "--git-dir", str(git_dir), "diff-tree", "-r", "-c", "--root",
         "--no-renames", "--name-only", "--no-commit-id", "-z", commit],
        check=True, capture_output=True, text=True,
    ).stdout
    return [path for path in out.split("\0") if path]


def test_batched_paths_match_one_commit_at_a_time(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    root = _edit(repo, "README.md", message="root")
    _git(repo, "checkout", "-b", "side")
    _edit(repo, "odd dir/a:b c.txt", message="side")
    _git(repo, "checkout", "main")
    _edit(repo, "infra/prod.tf", message="mainline")
    _git(repo, "merge", "--no-edit", "side")  # clean merge: nothing of its own
    _git(repo, "checkout", "-b", "evil", "HEAD~1")
    _edit(repo, "infra/prod.tf", message="conflicting")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-edit", "-X", "ours", "evil")
    _edit(repo, "infra/prod.tf", message="hand-edited merge result")
    _git(repo, "commit", "--allow-empty", "-m", "empty")
    # A path spelled exactly like another commit's ID stays a path.
    _edit(repo, root, message="named after the root commit")
    git_dir = git_objects.absolute_git_dir(repo)
    commits = _git(repo, "rev-list", "--all").split()

    batched = git_objects.paths_by_commit(git_dir, commits)
    assert batched == {
        commit: _paths_one_commit_at_a_time(git_dir, commit) for commit in commits
    }
    assert root in {path for paths in batched.values() for path in paths}
    assert git_objects.paths_by_commit(git_dir, []) == {}


def test_path_derivation_runs_a_fixed_number_of_git_processes(tmp_path, monkeypatch):
    """A push's cost in Git processes does not grow with its commit count."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    base = head = _edit(repo, "README.md", message="base")
    for i in range(40):
        head = _edit(repo, f"src/file{i % 7}.py", message=f"commit {i}")
    git_dir = git_objects.absolute_git_dir(repo)

    calls = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_objects.subprocess, "run", counting_run)
    paths, scope_paths = git_objects.changed_paths_against(
        git_dir, git_objects.ZERO_OID, head, [[base], []]
    )
    assert paths == [f"src/file{i}.py" for i in range(7)]
    assert scope_paths == ["README.md", *paths]
    # One commit walk per baseline plus one diff-tree, not one per commit.
    assert len(calls) == 3


def test_pre_receive_reads_pushed_objects_before_reserving_the_store(
    committed_conn, tmp_path, monkeypatch
):
    """Inspecting a push must not hold every other agent's command up."""
    conn = committed_conn
    repo_name, hub, clone, agent_a, _ = _setup(conn, tmp_path)
    _git(clone, "checkout", "-b", "feature")
    newrev = _edit(clone, "docs/new.md", message="feature")
    # Put the objects in the hub without a ref (or any hook), as a push's
    # quarantine would before pre-receive runs.
    subprocess.run(
        ["git", "--git-dir", str(hub), "-c", "core.hooksPath=/dev/null",
         "fetch", "--quiet", str(clone), "feature"],
        check=True, capture_output=True,
    )

    reserved_while_reading = []
    real_update_paths = gate.update_paths

    def watching_update_paths(*args, **kwargs):
        reserved_while_reading.append(conn.in_transaction)
        return real_update_paths(*args, **kwargs)

    monkeypatch.setattr(gate, "update_paths", watching_update_paths)
    monkeypatch.setenv("GIT_DIR", str(hub))
    monkeypatch.setenv("QUORUMGIT_AGENT", agent_a)
    zero = git_objects.zero_oid_like(newrev)
    status = gate.run_pre_receive(
        conn, repo_name, [f"{zero} {newrev} refs/heads/feature\n"]
    )

    assert status == 0
    assert reserved_while_reading == [False]
    recorded = conn.execute(
        "SELECT paths FROM ref_updates WHERE refname = 'refs/heads/feature' "
        "AND newrev = ?",
        (newrev,),
    ).fetchone()
    assert recorded is not None and json.loads(recorded[0]) == ["docs/new.md"]


# ------------------------------------------------------------ claimed scopes


def test_push_outside_claimed_scope_requires_approval(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    claim_id = _claimed_branch(conn, repo_name, a, "feat/api", ["src/api/**"])
    _git(clone, "checkout", "-b", "feat/api")

    _edit(clone, "src/api/routes.py", message="in scope")
    inside = _push(clone, a, "feat/api", cfg=cfg)
    assert inside.returncode == 0, inside.stderr

    _edit(
        clone,
        "src/api/routes.py",
        "src/database/schema.py",
        "infrastructure/prod.tf",
        message="strays",
    )
    stray = _push(clone, a, "feat/api", cfg=cfg)
    assert stray.returncode != 0
    assert "out_of_scope_push" in stray.stderr
    assert "src/database/schema.py" in stray.stderr
    assert "infrastructure/prod.tf" in stray.stderr

    plan = gate.prepare_push(conn, repo_name, "feat/api", clone, pusher=a)
    assert plan["refusals"] == []
    required = _required(plan)
    assert set(required) == {"out_of_scope_push"}
    operation = required["out_of_scope_push"]["operation"]
    assert operation["claim_id"] == claim_id
    assert operation["paths"] == ["infrastructure/prod.tf", "src/database/schema.py"]
    # The planner derives exactly the hash the hook demanded.
    assert required["out_of_scope_push"]["hash"] in stray.stderr

    approve(conn, operation, requested_by=a)
    conn.commit()
    accepted = _push(clone, a, "feat/api", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
    consumed = gate.get_approval(conn, required["out_of_scope_push"]["hash"])
    assert consumed["status"] == "consumed"


def test_merging_mainline_into_a_claimed_branch_stays_in_scope(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _claimed_branch(conn, repo_name, a, "feat/api", ["src/api/**"])
    _git(clone, "checkout", "-b", "feat/api")
    _edit(clone, "src/api/routes.py", message="feature")
    assert _push(clone, a, "feat/api", cfg=cfg).returncode == 0

    # Mainline moves on elsewhere, outside the claim's scope.
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(hub), str(other))
    _edit(other, "infra/prod.tf", message="mainline")
    op = {
        "type": "protected_ref_update",
        "repository": repo_name,
        "refname": "refs/heads/main",
        "oldrev": _git(hub, "rev-parse", "refs/heads/main"),
        "newrev": _git(other, "rev-parse", "HEAD"),
    }
    approve(conn, op, requested_by=a)
    conn.commit()
    assert _push(other, a, "main", cfg=cfg).returncode == 0

    _git(clone, "fetch", "origin")
    _git(clone, "merge", "--no-edit", "origin/main")
    merged = _push(clone, a, "feat/api", cfg=cfg)
    assert merged.returncode == 0, merged.stderr


def test_staging_work_on_another_branch_does_not_escape_the_claim(
    committed_conn, tmp_path, cfg
):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _claimed_branch(conn, repo_name, a, "feat/docs", ["docs/**"])
    _git(clone, "checkout", "-b", "feat/docs")
    _edit(clone, "src/auth.py", message="stray")

    direct = _push(clone, a, "feat/docs", cfg=cfg)
    assert direct.returncode != 0
    assert "out_of_scope_push" in direct.stderr

    # Unclaimed branches carry no scopes, so this is accepted...
    staged = _push(clone, a, "HEAD:refs/heads/scratch", cfg=cfg)
    assert staged.returncode == 0, staged.stderr
    # ...but the same commit still counts when it reaches the claimed branch.
    plan = gate.prepare_push(conn, repo_name, "feat/docs", clone, pusher=a)
    required = _required(plan)
    assert set(required) == {"out_of_scope_push"}
    assert required["out_of_scope_push"]["operation"]["paths"] == ["src/auth.py"]
    bypass = _push(clone, a, "feat/docs", cfg=cfg)
    assert bypass.returncode != 0
    assert "src/auth.py" in bypass.stderr
    # The planner derives exactly the hash the hook demanded.
    assert required["out_of_scope_push"]["hash"] in bypass.stderr


def test_new_claimed_branch_is_measured_from_the_default_branch(
    committed_conn, tmp_path, cfg
):
    """Without protected refs, the hub's default branch is still the mainline,
    so a new claimed branch is charged only for its own commits."""
    conn = committed_conn
    suffix = uuid.uuid4().hex[:8]
    hub = tmp_path / "open-hub.git"
    seed = tmp_path / "open-seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _edit(seed, "infra/prod.tf", "README.md", message="history outside the scope")
    _git(tmp_path, "clone", "--bare", str(seed), str(hub))
    repo_name = f"open-{suffix}"
    registry.add_repository(conn, repo_name, hub)
    agent = f"agent-{suffix}"
    registry.add_agent(conn, agent)
    gate.install_hook(conn, repo_name)
    conn.commit()
    assert registry.get_repository(conn, repo_name)["protected_refs"] == []

    _claimed_branch(conn, repo_name, agent, "feat/api", ["src/api/**"])
    clone = tmp_path / "open-clone"
    _git(tmp_path, "clone", str(hub), str(clone))
    _git(clone, "checkout", "-b", "feat/api")
    _edit(clone, "src/api/routes.py", message="feature")

    plan = gate.prepare_push(conn, repo_name, "feat/api", clone, pusher=agent)
    assert plan["operations"] == []
    pushed = _push(clone, agent, "feat/api", cfg=cfg)
    assert pushed.returncode == 0, pushed.stderr


# ----------------------------------------------------------- protected paths


def test_protected_paths_need_approval_on_any_branch(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, _a, b = _setup(conn, tmp_path)
    ensure_agent(conn, OPERATOR, "operator")
    registry.set_protected_path(
        conn, repo_name, ".github/workflows/**", actor=OPERATOR
    )
    conn.commit()

    _git(clone, "checkout", "-b", "feat/ci")
    _edit(clone, ".github/workflows/ci.yml", "src/app.py", message="ci")
    rejected = _push(clone, b, "feat/ci", cfg=cfg)
    assert rejected.returncode != 0
    assert "protected_path_update" in rejected.stderr

    plan = gate.prepare_push(conn, repo_name, "feat/ci", clone)
    required = _required(plan)
    assert set(required) == {"protected_path_update"}
    assert required["protected_path_update"]["operation"]["paths"] == [
        ".github/workflows/ci.yml"
    ]

    approve(conn, required["protected_path_update"]["operation"], requested_by=b)
    conn.commit()
    accepted = _push(clone, b, "feat/ci", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr


def test_protected_path_changes_are_operator_only(conn, approval_repo):
    ensure_agent(conn, OPERATOR, "operator")
    ensure_agent(conn, "a-worker")
    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_protected_path(conn, approval_repo, "infra/**", actor="a-worker")
    with pytest.raises(registry.RegistryError, match="requires an operator"):
        registry.set_allowed_ref_namespace(
            conn, approval_repo, "refs/tags/", actor="a-worker"
        )
    assert registry.set_protected_path(
        conn, approval_repo, "infra/**", actor=OPERATOR
    ) == ["infra/**"]
    assert registry.set_protected_path(
        conn, approval_repo, "infra/**", actor=OPERATOR, remove=True
    ) == []
    with pytest.raises(registry.RegistryError, match="refs/<name>/"):
        registry.set_allowed_ref_namespace(
            conn, approval_repo, "refs/tags", actor=OPERATOR
        )


# ------------------------------------------------------------- ref namespaces


def test_refs_outside_allowed_namespaces_are_refused(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    assert registry.get_repository(conn, repo_name)["allowed_ref_namespaces"] == [
        "refs/heads/"
    ]
    _git(clone, "tag", "v1")

    refused = _push(clone, a, "refs/tags/v1", cfg=cfg)
    assert refused.returncode != 0
    assert "allowed ref namespaces" in refused.stderr
    custom = _push(clone, a, "HEAD:refs/custom/x", cfg=cfg)
    assert custom.returncode != 0

    ensure_agent(conn, OPERATOR, "operator")
    registry.set_allowed_ref_namespace(conn, repo_name, "refs/tags/", actor=OPERATOR)
    conn.commit()
    allowed = _push(clone, a, "refs/tags/v1", cfg=cfg)
    assert allowed.returncode == 0, allowed.stderr


# ------------------------------------------------------------- push planning


def test_prepare_cli_requests_every_required_approval(
    committed_conn, tmp_path, cfg, monkeypatch, capsys
):
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    ensure_agent(conn, OPERATOR, "operator")
    registry.set_protected_path(conn, repo_name, "SECURITY.md", actor=OPERATOR)
    conn.commit()
    _edit(clone, "SECURITY.md", message="policy")

    monkeypatch.setenv("QUORUMGIT_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("QUORUMGIT_AGENT", a)
    code = cli.main([
        "approve", "prepare", "--repo", repo_name, "--ref", "main",
        "-C", str(clone), "--request", "--json",
    ])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["oldrev"] == _git(hub, "rev-parse", "refs/heads/main")
    assert plan["newrev"] == _git(clone, "rev-parse", "HEAD")
    assert plan["paths"] == ["SECURITY.md"]
    required = _required(plan)
    assert set(required) == {"protected_ref_update", "protected_path_update"}

    # One push needs both approvals; with only one granted it is refused and
    # nothing is consumed.
    for entry in required.values():
        assert entry["approval"]["status"] == "pending"
    gate.vote(conn, required["protected_ref_update"]["approval"]["id"], OPERATOR, True)
    conn.commit()
    partial = _push(clone, a, "main", cfg=cfg)
    assert partial.returncode != 0
    assert "protected_path_update" in partial.stderr
    assert gate.get_approval(
        conn, required["protected_ref_update"]["hash"]
    )["status"] == "approved"

    gate.vote(conn, required["protected_path_update"]["approval"]["id"], OPERATOR, True)
    conn.commit()
    accepted = _push(clone, a, "main", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
    for entry in required.values():
        assert gate.get_approval(conn, entry["hash"])["status"] == "consumed"


def test_prepare_reports_refusals_without_writing(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, b = _setup(conn, tmp_path)
    _claimed_branch(conn, repo_name, a, "feat/owned", ["src/**"])
    _git(clone, "checkout", "-b", "feat/owned")
    _edit(clone, "src/app.py", message="work")

    plan = gate.prepare_push(conn, repo_name, "feat/owned", clone, pusher=b)
    assert any("claimed by" in refusal for refusal in plan["refusals"])
    assert plan["operations"] == []

    tag_plan = gate.prepare_push(conn, repo_name, "refs/tags/v9", clone)
    assert any("allowed ref namespaces" in r for r in tag_plan["refusals"])

    deletion = gate.prepare_push(conn, repo_name, "main", clone, None)
    assert [e["operation"]["type"] for e in deletion["operations"]] == [
        "protected_ref_update"
    ]
    assert deletion["paths"] == []


# ------------------------------------------------- review regressions (#23)


def test_prepare_refuses_a_clone_missing_hub_tips(committed_conn, tmp_path, cfg):
    """A tip the clone lacks would make already-governed commits look new."""
    conn = committed_conn
    repo_name, hub, clone, a, _b = _setup(conn, tmp_path)
    _git(clone, "checkout", "-b", "feat/other")
    _edit(clone, "docs/other.md", message="other")
    assert _push(clone, a, "feat/other", cfg=cfg).returncode == 0

    narrow = tmp_path / "narrow"
    _git(
        tmp_path, "clone", "--no-local", "--single-branch", "--branch", "main",
        str(hub), str(narrow),
    )
    _git(narrow, "checkout", "-b", "feat/mine")
    _edit(narrow, "src/mine.py", message="mine")
    with pytest.raises(gate.GateError, match="lacks 1 commit"):
        gate.prepare_push(conn, repo_name, "feat/mine", narrow)

    _git(narrow, "fetch", "origin", "feat/other")
    plan = gate.prepare_push(conn, repo_name, "feat/mine", narrow)
    assert plan["paths"] == ["src/mine.py"]


def _sha256_supported(tmp_path: Path) -> bool:
    probe = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(tmp_path / "probe")],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def test_prepare_matches_the_hook_in_sha256_repositories(
    committed_conn, tmp_path, cfg
):
    if not _sha256_supported(tmp_path):
        pytest.skip("git without SHA-256 object format support")
    conn = committed_conn
    seed = tmp_path / "seed256"
    _git(tmp_path, "init", "-q", "-b", "main", "--object-format=sha256", str(seed))
    _edit(seed, "README.md", message="seed")
    hub = tmp_path / "hub256.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(hub))
    repo_name = f"hub256-{uuid.uuid4().hex[:8]}"
    registry.add_repository(
        conn, repo_name, hub, protected_refs=["refs/heads/main"],
        protected_paths=["secrets/**"],
    )
    agent = f"agent-256-{uuid.uuid4().hex[:8]}"
    registry.add_agent(conn, agent)
    gate.install_hook(conn, repo_name)
    conn.commit()
    clone = tmp_path / "clone256"
    _git(tmp_path, "clone", str(hub), str(clone))

    _git(clone, "checkout", "-b", "feat/new")
    _edit(clone, "secrets/key.txt", message="new ref")
    rejected = _push(clone, agent, "feat/new", cfg=cfg)
    assert rejected.returncode != 0
    plan = gate.prepare_push(conn, repo_name, "feat/new", clone)
    assert plan["oldrev"] == "0" * 64
    entry = _required(plan)["protected_path_update"]
    assert entry["hash"] in rejected.stderr

    deletion = gate.prepare_push(conn, repo_name, "main", clone, None)
    assert deletion["newrev"] == "0" * 64


@pytest.mark.skipif(
    sys.platform != "linux", reason="needs a filesystem that accepts non-UTF-8 names"
)
def test_non_utf8_paths_can_be_approved(committed_conn, tmp_path, cfg):
    conn = committed_conn
    repo_name, _hub, clone, a, _b = _setup(conn, tmp_path)
    _claimed_branch(conn, repo_name, a, "feat/bytes", ["src/**"])
    _git(clone, "checkout", "-b", "feat/bytes")
    with open(os.fsencode(clone) + b"/caf\xe9.txt", "wb") as handle:
        handle.write(b"bytes\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "non-utf8 name")

    rejected = _push(clone, a, "feat/bytes", cfg=cfg)
    assert rejected.returncode != 0
    assert "out_of_scope_push" in rejected.stderr
    plan = gate.prepare_push(conn, repo_name, "feat/bytes", clone)
    entry = _required(plan)["out_of_scope_push"]
    assert entry["operation"]["paths"] == ["caf\\xe9.txt"]
    assert entry["hash"] in rejected.stderr

    approve(conn, entry["operation"], requested_by=a)
    conn.commit()
    accepted = _push(clone, a, "feat/bytes", cfg=cfg)
    assert accepted.returncode == 0, accepted.stderr
