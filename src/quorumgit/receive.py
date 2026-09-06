"""Receive-hook protocol for exact Git mutation reservation and completion.

Pre-receive validates current governance and durably reserves each exact ref
transition without consuming its approval. Post-receive runs only after Git has
updated refs, revalidates the resulting ref, consumes any bound approval, and
marks the reservation completed in the same database transaction.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import audit, gate, mutations
from .registry import RegistryError, get_agent
from .store import Connection, begin_immediate
from .work import live_claim_for_branch, open_handoff_for_branch

POST_HOOK_MARKER = "# quorumgit-managed-post-receive v1"


class ReceiveError(gate.GateError):
    pass


def _registered_pusher(conn: Connection) -> dict:
    pusher = os.environ.get("QUORUMGIT_AGENT") or None
    if pusher is None:
        raise ReceiveError(
            "Pusher identity required; set QUORUMGIT_AGENT to a registered agent."
        )
    try:
        return get_agent(conn, pusher)
    except RegistryError as exc:
        raise ReceiveError(f"Pusher identity is not registered: {pusher}") from exc


def _git_dir() -> str:
    git_dir = os.environ.get("GIT_DIR")
    if not git_dir:
        raise ReceiveError("GIT_DIR is not set.")
    return git_dir


def _policy_for_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str,
    oldrev: str,
    newrev: str,
    refname: str,
) -> tuple[dict, dict | None]:
    repo = gate._verify_repository_binding(conn, repository, git_dir)
    branch = refname.removeprefix("refs/heads/")

    if refname.startswith("refs/heads/"):
        pending = open_handoff_for_branch(conn, repo["id"], branch)
        if pending:
            raise ReceiveError(
                f"Branch {branch!r} is frozen pending handoff "
                f"{pending['id']}; accept the handoff before pushing."
            )
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim and claim["agent"] != pusher:
            raise ReceiveError(
                f"Branch {branch!r} is claimed by {claim['agent']} "
                f"(claim {claim['id']}); pusher is {pusher}."
            )

    protected = refname in repo["protected_refs"]
    deletion = gate._is_zero(newrev)
    forced = (
        not deletion
        and not gate._is_zero(oldrev)
        and not gate._is_fast_forward(git_dir, oldrev, newrev)
    )
    approval = None
    if protected or deletion or forced:
        operation = {
            "type": "protected_ref_update"
            if protected
            else ("ref_delete" if deletion else "force_update"),
            "repository": repository,
            "refname": refname,
            "oldrev": oldrev,
            "newrev": newrev,
        }
        approval = gate.approved_instance(conn, operation)
        if approval is None:
            raise ReceiveError(
                f"{operation['type']} on {refname} requires an approval "
                f"bound to this exact update (hash {gate.operation_hash(operation)})."
            )
    return repo, approval


def run_pre_receive(
    conn: Connection,
    repository: str,
    stdin_lines: Iterable[str],
) -> int:
    """Validate and reserve each incoming ref mutation. Fail closed."""
    try:
        begin_immediate(conn)
        git_dir = _git_dir()
        pusher = _registered_pusher(conn)
        saw_update = False
        for line in stdin_lines:
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 3:
                raise ReceiveError(f"Malformed pre-receive input: {line!r}")
            saw_update = True
            oldrev, newrev, refname = parts
            repo, approval = _policy_for_update(
                conn,
                repository,
                git_dir,
                pusher["name"],
                oldrev,
                newrev,
                refname,
            )
            mutation = mutations.reserve(
                conn,
                repository_id=repo["id"],
                repository=repository,
                refname=refname,
                oldrev=oldrev,
                newrev=newrev,
                agent_id=pusher["id"],
                agent=pusher["name"],
                approval_id=approval["id"] if approval else None,
            )
            audit.record(
                conn,
                "gate.update_reserved",
                "repository",
                repo["id"],
                agent=pusher["name"],
                detail={
                    "mutation_id": mutation["id"],
                    "refname": refname,
                    "oldrev": oldrev,
                    "newrev": newrev,
                    "approval_id": approval["id"] if approval else None,
                },
            )
        if not saw_update:
            raise ReceiveError("No ref updates supplied on stdin.")
    except Exception as exc:
        conn.rollback()
        print(f"[quorumgit] REJECTED: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print("[quorumgit] reserved.")
    return 0


def _verify_ref_result(git_dir: str, refname: str, newrev: str) -> None:
    if gate._is_zero(newrev):
        result = subprocess.run(
            ["git", "--git-dir", git_dir, "show-ref", "--verify", "--quiet", refname],
            capture_output=True,
        )
        if result.returncode == 1:
            return
        if result.returncode == 0:
            raise ReceiveError(f"Git reports deleted ref {refname} still exists.")
        raise ReceiveError(f"Unable to verify deletion of {refname}.")

    result = subprocess.run(
        ["git", "--git-dir", git_dir, "rev-parse", "--verify", refname],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReceiveError(f"Unable to verify completed Git ref {refname}.")
    actual = result.stdout.strip()
    if actual != newrev:
        raise ReceiveError(
            f"Git ref {refname} is {actual}, expected completed mutation {newrev}."
        )


def run_post_receive(
    conn: Connection,
    repository: str,
    stdin_lines: Iterable[str],
) -> int:
    """Finalize reservations after Git has successfully updated its refs."""
    try:
        begin_immediate(conn)
        git_dir = _git_dir()
        pusher = _registered_pusher(conn)
        repo = gate._verify_repository_binding(conn, repository, git_dir)
        saw_update = False
        for line in stdin_lines:
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 3:
                raise ReceiveError(f"Malformed post-receive input: {line!r}")
            saw_update = True
            oldrev, newrev, refname = parts
            _verify_ref_result(git_dir, refname, newrev)
            mutation = mutations.reserved_exact(
                conn,
                repository_id=repo["id"],
                repository=repository,
                refname=refname,
                oldrev=oldrev,
                newrev=newrev,
                agent_id=pusher["id"],
            )
            if mutation is None:
                raise ReceiveError(
                    f"No live QuorumGit reservation matches completed ref {refname}."
                )
            if mutation["approval_id"] is not None:
                approval = gate.get_approval_by_id(conn, mutation["approval_id"])
                gate.consume_approval(
                    conn,
                    approval["id"],
                    approval["operation"],
                    agent=pusher["name"],
                )
            mutations.complete(conn, mutation["id"], agent=pusher["name"])
            audit.record(
                conn,
                "gate.update_completed",
                "repository",
                repo["id"],
                agent=pusher["name"],
                detail={
                    "mutation_id": mutation["id"],
                    "refname": refname,
                    "oldrev": oldrev,
                    "newrev": newrev,
                    "approval_id": mutation["approval_id"],
                },
            )
        if not saw_update:
            raise ReceiveError("No completed ref updates supplied on stdin.")
    except Exception as exc:
        conn.rollback()
        print(f"[quorumgit] COMPLETION ERROR: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print("[quorumgit] completed.")
    return 0


def _effective_hook(repository_path: str | Path, name: str) -> Path:
    repo_path = Path(repository_path).resolve()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-path", f"hooks/{name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise ReceiveError(f"Unable to resolve Git {name} hook path{suffix}")
    raw = result.stdout.strip()
    if not raw:
        raise ReceiveError(f"Git returned no {name} hook path.")
    hook = Path(raw)
    if not hook.is_absolute():
        hook = repo_path / hook
    return hook.resolve()


def _post_hook_script(repository: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{POST_HOOK_MARKER}\n"
        f"exec {shlex.quote(sys.executable)} -m quorumgit hook post-receive "
        f"--repo {shlex.quote(repository)}\n"
    )


def _preflight_post_hook(path: Path, expected: str) -> str:
    if not path.exists():
        return "installed"
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReceiveError(f"Cannot inspect existing post-receive hook {path}: {exc}") from exc
    if existing == expected:
        return "verified"
    if POST_HOOK_MARKER in existing.splitlines()[:3]:
        raise ReceiveError(
            f"Existing QuorumGit-managed post-receive hook differs at {path}; "
            "refusing to overwrite it."
        )
    raise ReceiveError(
        f"Existing post-receive hook at {path} is not owned by QuorumGit; "
        "refusing to overwrite or silently chain it."
    )


def install_hooks(conn: Connection, repository: str) -> Path:
    """Install the pre-receive gate plus its completion hook safely."""
    begin_immediate(conn)
    repo = gate.get_repository(conn, repository)
    post_path = _effective_hook(repo["path"], "post-receive")
    expected_post = _post_hook_script(repository)
    post_action = _preflight_post_hook(post_path, expected_post)

    # `gate.install_hook` performs the existing pre-receive ownership,
    # core.hooksPath, repository-identity, and atomic-write checks. Preflight
    # the post hook first so an unrelated post hook cannot leave a partial new
    # installation behind.
    pre_path = gate.install_hook(conn, repository)
    if post_action == "installed":
        gate._write_hook_atomically(post_path, expected_post)
    else:
        post_path.chmod(0o755)

    if _effective_hook(repo["path"], "post-receive") != post_path:
        raise ReceiveError("Installed post-receive hook is not Git's effective hook.")
    if post_path.read_text(encoding="utf-8") != expected_post:
        raise ReceiveError(f"Installed post-receive hook failed verification: {post_path}")
    audit.record(
        conn,
        f"hook.post_receive_{post_action}",
        "repository",
        repo["id"],
        detail={"path": str(post_path)},
    )
    return pre_path
