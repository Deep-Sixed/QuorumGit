"""Approval gate and pre-receive enforcement.

Protected operations require an approval whose hash binds to the exact
operation payload. Enforcement is fail-closed: any hook error rejects the push.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import audit, work
from .canonical import stable_hash
from .registry import (
    RegistryError,
    approvers_for_repository,
    assert_repository_identity_unique,
    get_agent,
    get_repository,
    git_common_dir,
)
from .store import Connection, begin_immediate, json_dumps, json_loads
from .work import live_claim_for_branch, open_handoff_for_branch

DEFAULT_THRESHOLD = 1
HOOK_MARKER = "# quorumgit-managed-pre-receive v1"


class GateError(RuntimeError):
    pass


class PushRejected(GateError):
    pass


# ---------------------------------------------------------------- approvals


def operation_hash(operation: dict[str, Any]) -> str:
    if not operation.get("type") or not operation.get("repository"):
        raise GateError("Operation requires 'type' and 'repository' fields.")
    return stable_hash(operation)


def _validate_takeover_operation(conn: Connection, operation: dict[str, Any]) -> None:
    if operation.get("type") != "lease_takeover":
        return
    required = ("task_id", "from_claim_id", "from_agent", "to_agent")
    missing = [field for field in required if operation.get(field) is None]
    if missing:
        raise GateError(f"Lease takeover operation is missing fields: {missing}")
    get_agent(conn, str(operation["to_agent"]))
    row = conn.execute(
        """
        SELECT 1
        FROM claims c
        JOIN agents a ON a.id = c.agent_id
        JOIN tasks t ON t.id = c.task_id
        JOIN repositories r ON r.id = t.repository_id
        WHERE c.id = ? AND c.task_id = ? AND a.name = ? AND r.name = ?
          AND c.released_at IS NULL AND c.lease_expires_at >= unixepoch()
        """,
        (
            operation["from_claim_id"],
            operation["task_id"],
            operation["from_agent"],
            operation["repository"],
        ),
    ).fetchone()
    if row is None:
        raise GateError(
            "Lease takeover operation does not match the current live incumbent claim."
        )


def _approval_dict(row) -> dict:
    return {
        "id": row[0],
        "operation_hash": row[1],
        "operation": json_loads(row[2], {}),
        "threshold": row[3],
        "status": row[4],
        "consumed_at": row[5],
    }


def quorum_threshold(approver_count: int) -> int:
    """Integer-stable 2/3 + 1 quorum over an approver roster."""
    return (2 * approver_count) // 3 + 1


def approval_policy(conn: Connection, repository: str) -> dict:
    """The approval policy governing operations on a repository.

    Operations naming an unregistered repository, or a repository with no
    roster and separation of duties off, keep the open policy: any registered
    agent may vote and the requested threshold applies.
    """
    row = conn.execute(
        "SELECT id, separate_duties FROM repositories WHERE name = ?",
        (repository,),
    ).fetchone()
    if row is None:
        return {"approvers": [], "separate_duties": False}
    return {
        "approvers": approvers_for_repository(conn, row[0]),
        "separate_duties": bool(row[1]),
    }


def required_threshold(
    requested: int, policy: dict, excluded: set[str] | frozenset[str] = frozenset()
) -> int:
    """A roster sets a 2/3 + 1 floor; a request may only raise it.

    The quorum is taken over the approvers eligible to vote on this approval,
    so separation of duties never makes approval arithmetically impossible
    for a roster that still has other members.
    """
    if policy["approvers"]:
        eligible = set(policy["approvers"]) - set(excluded)
        return max(requested, quorum_threshold(len(eligible)))
    return requested


def _requester(conn: Connection, approval_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT a.name FROM approvals ap
        JOIN agents a ON a.id = ap.requested_by_agent_id
        WHERE ap.id = ?
        """,
        (approval_id,),
    ).fetchone()
    return row[0] if row else None


def _excluded(
    conn: Connection, approval: dict, policy: dict, consumer: str | None = None
) -> set[str]:
    """Agents whose approval does not count under separation of duties."""
    if not policy["separate_duties"]:
        return set()
    requester = _requester(conn, approval["id"])
    return {name for name in (requester, consumer) if name}


def _tally(
    conn: Connection,
    approval: dict,
    policy: dict,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Yes votes that count under the current policy, and all deny votes.

    Recomputed on every decision and again at consumption, so removing an
    approver withdraws their vote and separation of duties discounts the
    consuming agent's own approval.
    """
    rows = conn.execute(
        "SELECT voter, vote FROM votes WHERE approval_id = ? ORDER BY voter",
        (approval["id"],),
    ).fetchall()
    roster = set(policy["approvers"])
    yes = [
        voter
        for voter, choice in rows
        if choice == 1
        and (not roster or voter in roster)
        and voter not in excluded
    ]
    no = [voter for voter, choice in rows if choice == 0]
    return yes, no


def request_approval(
    conn: Connection,
    operation: dict[str, Any],
    requested_by: str,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict:
    """Create or return the live approval instance for an exact operation.

    Pending/approved instances are reused. Denied/consumed instances are
    terminal history, so the same exact operation may be requested again as a
    fresh approval instance. BEGIN IMMEDIATE makes that lifecycle race-free.
    """
    begin_immediate(conn)
    requester = get_agent(conn, requested_by)
    _validate_takeover_operation(conn, operation)
    op_hash = operation_hash(operation)
    existing = conn.execute(
        """
        SELECT id, operation_hash, operation, threshold, status, consumed_at
        FROM approvals
        WHERE operation_hash = ? AND status IN ('pending', 'approved')
        ORDER BY id DESC
        LIMIT 1
        """,
        (op_hash,),
    ).fetchone()
    if existing is not None:
        return _approval_dict(existing)

    row = conn.execute(
        """
        INSERT INTO approvals (
            operation_hash, operation, threshold, requested_by_agent_id
        )
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (op_hash, json_dumps(operation), threshold, requester["id"]),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        "approval.requested",
        "approval",
        row[0],
        agent=requested_by,
        detail={"operation": operation, "hash": op_hash},
    )
    return get_approval(conn, op_hash)


def get_approval(conn: Connection, op_hash: str) -> dict:
    """Return the newest approval instance for an operation hash."""
    row = conn.execute(
        """
        SELECT id, operation_hash, operation, threshold, status, consumed_at
        FROM approvals
        WHERE operation_hash = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (op_hash,),
    ).fetchone()
    if row is None:
        raise GateError(f"No approval request exists for {op_hash}")
    return _approval_dict(row)


def get_approval_by_id(conn: Connection, approval_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, operation_hash, operation, threshold, status, consumed_at
        FROM approvals WHERE id = ?
        """,
        (approval_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"No approval request exists with id {approval_id}")
    return _approval_dict(row)


def vote(conn: Connection, approval_id: int, voter: str, approve: bool) -> dict:
    """Record a vote against one explicit approval instance atomically.

    BEGIN IMMEDIATE serializes competing voters before either reads the current
    approval state. Denial has precedence and terminal states remain final. An
    approved but unused approval still accepts votes, so a deny revokes it
    before use and extra yes votes can restore quorum after a roster change.
    """
    begin_immediate(conn)
    voter_row = get_agent(conn, voter)
    approval = get_approval_by_id(conn, approval_id)
    if approval["status"] not in ("pending", "approved"):
        raise GateError(f"Approval {approval_id} is already {approval['status']}.")
    repository = str(approval["operation"].get("repository"))
    policy = approval_policy(conn, repository)
    if policy["approvers"] and voter not in policy["approvers"]:
        raise GateError(
            f"Agent {voter} is not an approver for repository {repository}."
        )
    if policy["separate_duties"] and voter == _requester(conn, approval["id"]):
        raise GateError(
            f"Agent {voter} requested approval {approval_id}; separation of "
            f"duties on repository {repository} forbids voting on it."
        )
    conn.execute(
        """
        INSERT INTO votes (approval_id, voter, vote, voter_agent_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (approval_id, voter) DO UPDATE SET
            vote = excluded.vote,
            voter_agent_id = excluded.voter_agent_id
        """,
        (approval["id"], voter, approve, voter_row["id"]),
    )
    audit.record(
        conn,
        "approval.vote",
        "approval",
        approval["id"],
        agent=voter,
        detail={"vote": approve, "hash": approval["operation_hash"]},
    )

    excluded = _excluded(conn, approval, policy)
    yes, no = _tally(conn, approval, policy, excluded)
    if no:
        new_status = "denied"
    elif len(yes) >= required_threshold(approval["threshold"], policy, excluded):
        new_status = "approved"
    else:
        new_status = "pending"
    if new_status != approval["status"]:
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = unixepoch() "
            "WHERE id = ? AND status IN ('pending', 'approved')",
            (new_status, approval["id"]),
        )
        audit.record(
            conn,
            f"approval.{new_status}",
            "approval",
            approval["id"],
            detail={"hash": approval["operation_hash"]},
        )
    return get_approval_by_id(conn, approval_id)


def is_approved(conn: Connection, operation: dict[str, Any]) -> bool:
    """True only if the newest instance for this exact operation is approved."""
    try:
        approval = get_approval(conn, operation_hash(operation))
    except GateError:
        return False
    return approval["status"] == "approved" and approval["operation"] == operation


def approved_instance(conn: Connection, operation: dict[str, Any]) -> dict | None:
    try:
        approval = get_approval(conn, operation_hash(operation))
    except GateError:
        return None
    if approval["status"] == "approved" and approval["operation"] == operation:
        return approval
    return None


def consume_approval(
    conn: Connection, approval_id: int, operation: dict[str, Any], agent: str
) -> None:
    """Atomically consume one approved exact-operation authorization."""
    begin_immediate(conn)
    consumer = get_agent(conn, agent)
    op_hash = operation_hash(operation)
    approval = get_approval_by_id(conn, approval_id)
    if approval["operation_hash"] != op_hash or approval["operation"] != operation:
        raise GateError(
            f"Approval {approval_id} is not bound to the requested operation."
        )
    if approval["status"] != "approved":
        raise GateError(
            f"Approval {op_hash} is not consumable (status "
            f"{approval['status']}); it may already be used."
        )
    policy = approval_policy(conn, str(operation["repository"]))
    excluded = _excluded(conn, approval, policy, consumer=agent)
    yes, _no = _tally(conn, approval, policy, excluded)
    required = required_threshold(approval["threshold"], policy, excluded)
    if len(yes) < required:
        detail = ""
        if policy["separate_duties"]:
            detail = f"; {agent}'s own vote and the requester's do not count"
        raise GateError(
            f"Approval {approval_id} does not satisfy the current approval "
            f"policy for {operation['repository']}: {len(yes)} eligible yes "
            f"vote(s), {required} required{detail}."
        )
    cur = conn.execute(
        "UPDATE approvals SET status = 'consumed', consumed_at = unixepoch(), "
        "consumed_by_agent_id = ? "
        "WHERE id = ? AND status = 'approved'",
        (consumer["id"], approval["id"]),
    )
    if cur.rowcount != 1:
        raise GateError(
            f"Approval {approval_id} was consumed concurrently; refusing to "
            "authorize twice."
        )
    audit.record(
        conn,
        "approval.consumed",
        "approval",
        approval["id"],
        agent=agent,
        detail={"hash": op_hash},
    )


# --------------------------------------------------------------- hook logic


ZERO_OID = "0" * 40


def _is_zero(oid: str) -> bool:
    return set(oid) == {"0"}


def _is_fast_forward(
    git_dir: str, oldrev: str, newrev: str, env: dict[str, str] | None = None
) -> bool:
    result = subprocess.run(
        [
            "git",
            "--git-dir",
            git_dir,
            "merge-base",
            "--is-ancestor",
            oldrev,
            newrev,
        ],
        capture_output=True,
        check=False,
        env=env,
    )
    if result.returncode not in (0, 1):
        raise PushRejected("Unable to determine fast-forward status.")
    return result.returncode == 0


def _new_commit_paths(
    git_dir: str, newrev: str, env: dict[str, str] | None = None
) -> list[str]:
    """Paths changed by the commits this push introduces.

    Only commits not yet reachable from any existing ref are inspected, so a
    branch created from (or merged with) already-published history is not
    charged for that history. Merge commits use Git's combined diff, which
    reports only paths the merge result changes relative to every parent:
    conflict resolutions and edits made inside the merge itself. Renames are
    split into a deletion and an addition so both paths are checked.
    """
    commits = subprocess.run(
        ["git", "--git-dir", git_dir, "rev-list", newrev, "--not", "--all"],
        capture_output=True,
        check=False,
        env=env,
    )
    if commits.returncode != 0:
        raise PushRejected("Unable to list the commits introduced by this push.")
    if not commits.stdout.strip():
        return []
    result = subprocess.run(
        [
            "git",
            "--git-dir",
            git_dir,
            "diff-tree",
            "--stdin",
            "-r",
            "-c",
            "-z",
            "--root",
            "--no-renames",
            "--no-commit-id",
            "--name-only",
        ],
        input=commits.stdout,
        capture_output=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise PushRejected("Unable to determine the paths changed by this push.")
    return sorted(
        {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
    )


def _invoking_git_common_dir(git_dir: str) -> Path:
    result = subprocess.run(
        ["git", "--git-dir", git_dir, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise PushRejected(f"Unable to resolve invoking Git repository{suffix}")
    raw = result.stdout.strip()
    if not raw:
        raise PushRejected("Git returned no common directory for invoking repository.")
    common = Path(raw)
    if not common.is_absolute():
        common = Path.cwd() / common
    return common.resolve()


def _verify_repository_binding(
    conn: Connection,
    repository: str,
    git_dir: str,
) -> dict:
    repo = get_repository(conn, repository)
    expected = assert_repository_identity_unique(conn, repo)
    actual = _invoking_git_common_dir(git_dir)
    if actual != expected:
        raise PushRejected(
            f"Hook repository mismatch: {repository!r} is registered for "
            f"{expected}, but this push is running in {actual}."
        )
    return repo


def governed_operation(
    conn: Connection,
    repo: dict,
    git_dir: str,
    pusher: str | None,
    oldrev: str,
    newrev: str,
    refname: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The approval-gated operation a ref update amounts to, if any.

    Raises PushRejected when branch reservations forbid the update outright
    (approvals never override them). Returns None for an ungoverned update.
    This is the single derivation shared by the pre-receive hook and
    `approve request --push`, so a request built before pushing binds to the
    exact payload the hook recomputes. `env` lets the offline derivation see
    the pusher's not-yet-pushed objects through an object alternate.
    """
    repository = repo["name"]
    branch = refname.removeprefix("refs/heads/")
    deletion = _is_zero(newrev)
    out_of_scope: list[str] = []

    if refname.startswith("refs/heads/"):
        pending = open_handoff_for_branch(conn, repo["id"], branch)
        if pending:
            raise PushRejected(
                f"Branch {branch!r} is frozen pending handoff "
                f"{pending['id']}; accept the handoff before pushing."
            )
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim and claim["agent"] != pusher:
            raise PushRejected(
                f"Branch {branch!r} is claimed by {claim['agent']} "
                f"(claim {claim['id']}); pusher is "
                f"{pusher or 'unidentified — set QUORUMGIT_AGENT'}."
            )
        if claim and not deletion:
            # Declared scopes are enforced on what the push actually changes,
            # not only on what the claim said it would change.
            out_of_scope = work.paths_outside_scopes(
                _new_commit_paths(git_dir, newrev, env),
                work.claim_scopes(conn, claim["id"]),
            )

    protected = refname in repo["protected_refs"]
    # Only branches are coordinated by claims; tags, notes, and custom refs
    # are governed by approval unless the repository opened their namespace.
    outside_branches = not refname.startswith("refs/heads/") and not any(
        refname.startswith(prefix) for prefix in repo["open_namespaces"]
    )
    forced = (
        not deletion
        and not _is_zero(oldrev)
        and not _is_fast_forward(git_dir, oldrev, newrev, env)
    )

    if not (protected or deletion or forced or out_of_scope or outside_branches):
        return None
    if protected:
        op_type = "protected_ref_update"
    elif deletion:
        op_type = "ref_delete"
    elif forced:
        op_type = "force_update"
    elif outside_branches:
        op_type = "non_branch_ref_update"
    else:
        op_type = "out_of_scope_update"
    operation: dict[str, Any] = {
        "type": op_type,
        "repository": repository,
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
    }
    if out_of_scope:
        operation["out_of_scope_paths"] = out_of_scope
    return operation


def _git_out(args: list[str], what: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GateError(f"{what}{': ' + detail if detail else ''}")
    return result.stdout.strip()


def derive_push_operation(
    conn: Connection,
    repository: str,
    refname: str,
    pusher: str,
    *,
    source: str | Path | None = None,
    rev: str = "HEAD",
    delete: bool = False,
) -> dict[str, Any] | None:
    """Derive, before pushing, the exact operation the hook will require.

    The hub's current value of the ref is the old revision; the new revision
    is resolved in the pusher's own repository (`source`). Nothing is fetched
    or written: the hub is inspected with the source's object store attached
    as a read-only alternate, the same way Git exposes incoming objects to a
    real pre-receive hook, and the hook's own derivation is reused. Returns
    None when the push needs no approval.
    """
    if not refname.startswith("refs/"):
        refname = f"refs/heads/{refname}"
    repo = get_repository(conn, repository)
    get_agent(conn, pusher)
    hub_git_dir = str(assert_repository_identity_unique(conn, repo))
    # Branches point at commits. Other refs (an annotated tag, say) are pushed
    # as the object itself, which is what the hook sees, so do not peel them.
    peel = "^{commit}" if refname.startswith("refs/heads/") else ""
    current = subprocess.run(
        ["git", "--git-dir", hub_git_dir, "rev-parse", "--verify", "--quiet",
         f"{refname}{peel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    exists = current.returncode == 0

    env: dict[str, str] | None = None
    if delete:
        if not exists:
            raise GateError(f"{refname} does not exist in {repository}; nothing to delete.")
        oldrev = current.stdout.strip()
        newrev = "0" * len(oldrev)
    else:
        if source is None:
            raise GateError("Deriving a push needs --from <your clone>.")
        source_path = Path(source).resolve()
        newrev = _git_out(
            ["-C", str(source_path), "rev-parse", "--verify", f"{rev}{peel}"],
            f"Cannot resolve {rev!r} in {source_path}",
        )
        try:
            source_common = git_common_dir(source_path)
        except RegistryError as exc:
            raise GateError(str(exc)) from exc
        env = {
            **os.environ,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source_common / "objects"),
        }
        oldrev = current.stdout.strip() if exists else "0" * len(newrev)
        if exists and oldrev == newrev:
            raise GateError(f"{refname} is already at {newrev}; nothing to push.")
    return governed_operation(
        conn, repo, hub_git_dir, pusher, oldrev, newrev, refname, env=env
    )


def check_ref_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str | None,
    oldrev: str,
    newrev: str,
    refname: str,
) -> None:
    """Enforce governance for one ref update. Raises PushRejected."""
    repo = _verify_repository_binding(conn, repository, git_dir)
    operation = governed_operation(
        conn, repo, git_dir, pusher, oldrev, newrev, refname
    )

    if operation is not None:
        approval = approved_instance(conn, operation)
        if approval is None:
            scope_note = ""
            out_of_scope = operation.get("out_of_scope_paths", [])
            if out_of_scope:
                shown = ", ".join(out_of_scope[:10])
                more = len(out_of_scope) - 10
                if more > 0:
                    shown += f", … (+{more} more)"
                scope_note = (
                    f" It changes paths outside the pusher's claimed scopes: "
                    f"{shown}."
                )
            if operation["type"] == "ref_delete":
                how = f"--push {refname} --delete"
            else:
                how = f"--push {refname} --from <your clone> --rev {newrev}"
            raise PushRejected(
                f"{operation['type']} on {refname} requires an approval "
                f"bound to this exact update (hash {operation_hash(operation)})."
                f"{scope_note} Request it with: quorumgit approve request "
                f"--repo {repository} {how}"
            )
        assert pusher is not None
        consume_approval(conn, approval["id"], operation, agent=pusher)
        audit.record(
            conn,
            "gate.protected_update_allowed",
            "repository",
            repo["id"],
            agent=pusher,
            detail=operation,
        )
        return

    audit.record(
        conn,
        "gate.update_allowed",
        "repository",
        repo["id"],
        agent=pusher,
        detail={"refname": refname, "oldrev": oldrev, "newrev": newrev},
    )


def run_pre_receive(
    conn: Connection, repository: str, stdin_lines: Iterable[str]
) -> int:
    """Hook entry point. Reads `oldrev newrev refname` lines. Fail-closed."""
    git_dir = os.environ.get("GIT_DIR")
    if not git_dir:
        print("[quorumgit] REJECTED: GIT_DIR is not set.", file=sys.stderr)
        return 1
    pusher = os.environ.get("QUORUMGIT_AGENT") or None
    try:
        begin_immediate(conn)
        if pusher is None:
            raise PushRejected(
                "Pusher identity required; set QUORUMGIT_AGENT to a registered agent."
            )
        try:
            get_agent(conn, pusher)
        except RegistryError as exc:
            raise PushRejected(f"Pusher identity is not registered: {pusher}") from exc
        saw_update = False
        for line in stdin_lines:
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 3:
                raise PushRejected(f"Malformed pre-receive input: {line!r}")
            saw_update = True
            check_ref_update(conn, repository, git_dir, pusher, *parts)
        if not saw_update:
            raise PushRejected("No ref updates supplied on stdin.")
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        conn.rollback()
        print(f"[quorumgit] REJECTED: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print("[quorumgit] accepted.")
    return 0


def _effective_pre_receive_hook(repository_path: str | Path) -> Path:
    repo_path = Path(repository_path).resolve()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-path", "hooks/pre-receive"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise GateError(f"Unable to resolve Git hook path for {repo_path}{suffix}")
    raw = result.stdout.strip()
    if not raw:
        raise GateError(f"Git returned no pre-receive hook path for {repo_path}")
    hook_path = Path(raw)
    if not hook_path.is_absolute():
        hook_path = repo_path / hook_path
    return hook_path.resolve()


def _hook_script(repository: str, executable: str | None = None) -> str:
    python = executable or sys.executable
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        f"exec {shlex.quote(python)} -m quorumgit hook pre-receive "
        f"--repo {shlex.quote(repository)}\n"
    )


def _legacy_hook_script(repository: str) -> str:
    """Exact pre-PR6 hook shape, recognized only for safe in-place upgrade."""
    return (
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -m quorumgit hook pre-receive '
        f'--repo "{repository}"\n'
    )


def _write_hook_atomically(hook_path: Path, content: str) -> None:
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{hook_path.name}.quorumgit-",
        dir=hook_path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o755)
        os.replace(tmp_path, hook_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def install_hook(conn: Connection, repository: str) -> Path:
    """Safely install QuorumGit at Git's effective pre-receive hook path.

    Existing unrelated hooks are never overwritten or silently chained. An
    exact current QuorumGit hook is idempotent; an exact legacy QuorumGit hook
    for the same repository is upgraded in place. Any other existing content
    is refused so operators must make coexistence explicit.
    """
    begin_immediate(conn)
    repo = get_repository(conn, repository)
    common_dir = assert_repository_identity_unique(conn, repo)
    hook_path = _effective_pre_receive_hook(repo["path"])
    expected = _hook_script(repository)
    action = "installed"

    if hook_path.exists():
        try:
            existing = hook_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise GateError(
                f"Cannot inspect existing pre-receive hook {hook_path}: {exc}"
            ) from exc
        if existing == expected:
            hook_path.chmod(0o755)
            action = "verified"
        elif existing == _legacy_hook_script(repository):
            _write_hook_atomically(hook_path, expected)
            action = "upgraded"
        elif HOOK_MARKER in existing.splitlines()[:3]:
            raise GateError(
                f"Existing QuorumGit-managed pre-receive hook differs at "
                f"{hook_path}; refusing to overwrite it."
            )
        else:
            raise GateError(
                f"Existing pre-receive hook at {hook_path} is not owned by "
                "QuorumGit; refusing to overwrite or silently chain it."
            )
    else:
        _write_hook_atomically(hook_path, expected)

    effective = _effective_pre_receive_hook(repo["path"])
    if effective != hook_path or not hook_path.exists():
        raise GateError(
            f"Installed hook is not Git's effective pre-receive hook: {hook_path}"
        )
    if hook_path.read_text(encoding="utf-8") != expected:
        raise GateError(f"Installed pre-receive hook failed verification: {hook_path}")

    audit.record(
        conn,
        f"hook.{action}",
        "repository",
        repo["id"],
        detail={
            "path": str(hook_path),
            "git_common_dir": str(common_dir),
        },
    )
    return hook_path
