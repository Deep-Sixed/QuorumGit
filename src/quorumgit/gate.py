"""Approval gate and pre-receive enforcement.

Protected operations require an approval whose hash binds to the exact
operation payload. Who may authorize is owned by the operation's repository:
its approval policy names the threshold, the roles whose votes count, and
whether the requester may vote. An agent that approved an operation may never
be the one that carries it out. Enforcement is fail-closed: any hook error
rejects the push.
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

from . import audit
from .canonical import stable_hash
from .registry import (
    RegistryError,
    approval_policy,
    assert_repository_identity_unique,
    get_agent,
    get_repository,
)
from .store import Connection, begin_immediate, json_dumps, json_loads
from .work import live_claim_for_branch, open_handoff_for_branch

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


_APPROVAL_COLUMNS = (
    "id, operation_hash, operation, threshold, status, consumed_at, "
    "repository_id, requested_by_agent_id"
)


def _approval_dict(row) -> dict:
    return {
        "id": row[0],
        "operation_hash": row[1],
        "operation": json_loads(row[2], {}),
        "threshold": row[3],
        "status": row[4],
        "consumed_at": row[5],
        "repository_id": row[6],
        "requested_by_agent_id": row[7],
    }


def _operation_repository(conn: Connection, operation: dict[str, Any]) -> dict:
    try:
        return get_repository(conn, str(operation["repository"]))
    except RegistryError as exc:
        raise GateError(
            f"Approval operations must name a registered repository: {exc}"
        ) from exc


def _policy_for(conn: Connection, approval: dict) -> dict:
    if approval["repository_id"] is None:
        raise GateError(
            f"Approval {approval['id']} predates repository approval policy "
            "and names no registered repository; request it again."
        )
    return approval_policy(conn, approval["repository_id"])


def _vote_refusal(approval: dict, policy: dict, voter: dict) -> str | None:
    """Why this agent may not vote on this approval, or None if it may."""
    if voter["role"] not in policy["roles"]:
        return (
            f"{voter['name']} is a {voter['role']}; this repository accepts "
            f"votes only from: {', '.join(policy['roles']) or 'nobody'}."
        )
    if (
        not policy["requester_may_vote"]
        and approval["requested_by_agent_id"] == voter["id"]
    ):
        return f"{voter['name']} requested this approval and may not vote on it."
    operation = approval["operation"]
    if (
        operation.get("type") == "lease_takeover"
        and operation.get("to_agent") == voter["name"]
    ):
        return f"{voter['name']} would receive this takeover and may not vote on it."
    return None


def _eligible_approvals(
    conn: Connection,
    approval: dict,
    policy: dict,
    exclude_agent_id: int | None = None,
) -> int:
    """Count yes votes that still satisfy the repository's current policy.

    Roles and policy can change after a vote is cast, so authority is
    re-derived from current state rather than trusted from the vote row.
    """
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.role
        FROM votes v JOIN agents a ON a.id = v.voter_agent_id
        WHERE v.approval_id = ? AND v.vote = 1
        """,
        (approval["id"],),
    ).fetchall()
    return sum(
        1
        for agent_id, name, role in rows
        if agent_id != exclude_agent_id
        and _vote_refusal(approval, policy, {"id": agent_id, "name": name, "role": role})
        is None
    )


def _authorization_refusal(
    conn: Connection, approval: dict, consumer: dict | None
) -> str | None:
    """Why an approved instance cannot authorize this consumer now, or None."""
    policy = _policy_for(conn, approval)
    if consumer is not None:
        approved_by_consumer = conn.execute(
            "SELECT 1 FROM votes WHERE approval_id = ? AND voter_agent_id = ? "
            "AND vote = 1",
            (approval["id"], consumer["id"]),
        ).fetchone()
        if approved_by_consumer is not None:
            return (
                f"{consumer['name']} approved this operation and may not also "
                "carry it out."
            )
    eligible = _eligible_approvals(conn, approval, policy)
    if eligible < policy["threshold"]:
        return (
            f"Approval {approval['id']} has {eligible} eligible approval(s); the "
            f"repository policy now requires {policy['threshold']}."
        )
    return None


def request_approval(
    conn: Connection,
    operation: dict[str, Any],
    requested_by: str,
) -> dict:
    """Create or return the live approval instance for an exact operation.

    Pending/approved instances are reused. Denied/consumed instances are
    terminal history, so the same exact operation may be requested again as a
    fresh approval instance. BEGIN IMMEDIATE makes that lifecycle race-free.

    The requester does not choose the quorum: the threshold is the policy of
    the repository the operation names, recorded on the instance for display
    and re-evaluated against current policy whenever it is used.
    """
    begin_immediate(conn)
    requester = get_agent(conn, requested_by)
    op_hash = operation_hash(operation)
    repo = _operation_repository(conn, operation)
    _validate_takeover_operation(conn, operation)
    existing = conn.execute(
        f"""
        SELECT {_APPROVAL_COLUMNS}
        FROM approvals
        WHERE operation_hash = ? AND status IN ('pending', 'approved')
        ORDER BY id DESC
        LIMIT 1
        """,
        (op_hash,),
    ).fetchone()
    if existing is not None:
        return _approval_dict(existing)

    threshold = approval_policy(conn, repo["id"])["threshold"]
    row = conn.execute(
        """
        INSERT INTO approvals (
            operation_hash, operation, threshold, requested_by_agent_id,
            repository_id
        )
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (op_hash, json_dumps(operation), threshold, requester["id"], repo["id"]),
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
        f"""
        SELECT {_APPROVAL_COLUMNS}
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
        f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"No approval request exists with id {approval_id}")
    return _approval_dict(row)


def vote(conn: Connection, approval_id: int, voter: str, approve: bool) -> dict:
    """Record a vote against one explicit approval instance atomically.

    BEGIN IMMEDIATE serializes competing voters before either reads the current
    approval state. Denial has precedence and terminal states remain final.
    Only agents the repository's policy recognizes may vote, in either
    direction; the requester may not vote unless the policy allows it.
    """
    begin_immediate(conn)
    voter_row = get_agent(conn, voter)
    approval = get_approval_by_id(conn, approval_id)
    if approval["status"] != "pending":
        raise GateError(f"Approval {approval_id} is already {approval['status']}.")
    policy = _policy_for(conn, approval)
    refusal = _vote_refusal(approval, policy, voter_row)
    if refusal is not None:
        raise GateError(f"Vote refused: {refusal}")
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

    denials = conn.execute(
        "SELECT count(*) FROM votes WHERE approval_id = ? AND vote = 0",
        (approval["id"],),
    ).fetchone()
    assert denials is not None
    if denials[0] > 0:
        new_status = "denied"
    elif _eligible_approvals(conn, approval, policy) >= policy["threshold"]:
        new_status = "approved"
    else:
        new_status = "pending"
    if new_status != "pending":
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = unixepoch() "
            "WHERE id = ? AND status = 'pending'",
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


def approved_instance(
    conn: Connection,
    operation: dict[str, Any],
    consumer: str | None = None,
) -> dict | None:
    """The approved instance for this exact operation, if it can be used now.

    With a consumer, the instance must also be usable by that agent: an agent
    that approved an operation cannot be the one that carries it out.
    """
    try:
        approval = get_approval(conn, operation_hash(operation))
    except GateError:
        return None
    if approval["status"] != "approved" or approval["operation"] != operation:
        return None
    try:
        consumer_row = get_agent(conn, consumer) if consumer else None
        if _authorization_refusal(conn, approval, consumer_row) is not None:
            return None
    except (GateError, RegistryError):
        return None
    return approval


def approval_refusal(
    conn: Connection, operation: dict[str, Any], consumer: str | None = None
) -> str | None:
    """Why an existing approved instance cannot be used, for error messages."""
    try:
        approval = get_approval(conn, operation_hash(operation))
    except GateError:
        return None
    if approval["status"] != "approved":
        return None
    try:
        consumer_row = get_agent(conn, consumer) if consumer else None
        return _authorization_refusal(conn, approval, consumer_row)
    except (GateError, RegistryError) as exc:
        return str(exc)


def is_approved(conn: Connection, operation: dict[str, Any]) -> bool:
    """True only if the newest instance for this exact operation is usable."""
    return approved_instance(conn, operation) is not None


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
    refusal = _authorization_refusal(conn, approval, consumer)
    if refusal is not None:
        raise GateError(f"Approval {approval_id} cannot authorize {agent}: {refusal}")
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


def _is_fast_forward(git_dir: str, oldrev: str, newrev: str) -> bool:
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
    )
    if result.returncode not in (0, 1):
        raise PushRejected("Unable to determine fast-forward status.")
    return result.returncode == 0


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
    branch = refname.removeprefix("refs/heads/")

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

    protected = refname in repo["protected_refs"]
    deletion = _is_zero(newrev)
    forced = (
        not deletion
        and not _is_zero(oldrev)
        and not _is_fast_forward(git_dir, oldrev, newrev)
    )

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
        approval = approved_instance(conn, operation, consumer=pusher)
        if approval is None:
            refusal = approval_refusal(conn, operation, consumer=pusher)
            raise PushRejected(
                f"{operation['type']} on {refname} requires an approval "
                f"bound to this exact update (hash {operation_hash(operation)})."
                + (f" {refusal}" if refusal else "")
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
