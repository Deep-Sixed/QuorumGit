"""Approval gate and pre-receive enforcement.

Protected operations require an approval whose hash binds to the exact
operation payload. What a push touches is derived from the Git objects it
carries (see git_objects), never from what an agent declared it would modify:
changes outside the pushing claim's scopes and changes to protected paths are
approval-governed just like protected refs, force pushes, and deletions. Who may authorize is owned by the operation's repository:
its approval policy names the threshold, the roles whose votes count, and
whether the requester may vote. An agent that approved an operation may never
be the one that carries it out. Enforcement is fail-closed: any hook error
rejects the push.
"""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import audit, git_objects
from .canonical import stable_hash
from .git_objects import is_zero, zero_oid_like
from .registry import (
    RegistryError,
    approval_policy,
    assert_repository_identity_unique,
    get_agent,
    get_repository,
    git_common_dir,
    path_scoped_git_env,
)
from .store import Connection, begin_immediate, json_dumps, json_loads
from .work import (
    claim_scopes,
    live_claim_for_branch,
    open_handoff_for_branch,
    paths_outside,
    paths_within,
)

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


def ref_namespace_refusal(repo: dict, refname: str) -> str | None:
    """Why this repository refuses any push to refname, or None."""
    allowed = repo["allowed_ref_namespaces"]
    if any(refname.startswith(prefix) for prefix in allowed):
        return None
    return (
        f"{refname} is outside {repo['name']!r}'s allowed ref namespaces "
        f"({', '.join(allowed) or 'none'}); an operator can allow it with "
        f"`quorumgit repo allow-ref {repo['name']} <prefix>`."
    )


def governed_operations(
    conn: Connection,
    repo: dict,
    refname: str,
    oldrev: str,
    newrev: str,
    git_dir: str | Path,
    paths: list[str],
) -> list[dict]:
    """Every approval one ref update needs, derived from what it carries.

    ``paths`` are the paths the update brings in (git_objects.changed_paths).
    Ref-level governance (protected ref, force push, deletion) comes first,
    then content governance: paths outside the scopes of the claim on the
    branch, and paths under the repository's protected paths. Each operation
    binds the exact revisions, so its hash is reproducible by anyone who can
    see the same objects — the hook and ``approve prepare`` alike.
    """
    base = {
        "repository": repo["name"],
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
    }
    operations: list[dict] = []
    deletion = is_zero(newrev)
    forced = (
        not deletion
        and not is_zero(oldrev)
        and not git_objects.is_ancestor(git_dir, oldrev, newrev)
    )
    if refname in repo["protected_refs"]:
        operations.append({"type": "protected_ref_update", **base})
    elif deletion:
        operations.append({"type": "ref_delete", **base})
    elif forced:
        operations.append({"type": "force_update", **base})

    if refname.startswith("refs/heads/") and paths:
        branch = refname.removeprefix("refs/heads/")
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim is not None:
            outside = paths_outside(paths, claim_scopes(conn, claim["id"]))
            if outside:
                operations.append({
                    "type": "out_of_scope_push",
                    **base,
                    "claim_id": claim["id"],
                    "paths": outside,
                })
    protected = paths_within(paths, repo["protected_paths"])
    if protected:
        operations.append({"type": "protected_path_update", **base, "paths": protected})
    return operations


def _describe_paths(paths: list[str], limit: int = 10) -> str:
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f", … ({len(paths) - limit} more)"
    return shown


def _requirement_message(operation: dict, refusal: str | None) -> str:
    message = (
        f"{operation['type']} on {operation['refname']} requires an approval "
        f"bound to this exact update (hash {operation_hash(operation)})."
    )
    if operation["type"] == "out_of_scope_push":
        message += (
            f" The push changes paths outside claim {operation['claim_id']}'s "
            f"scopes: {_describe_paths(operation['paths'])}."
        )
    elif operation.get("paths"):
        message += f" Paths: {_describe_paths(operation['paths'])}."
    if refusal:
        message += f" {refusal}"
    return message


def evaluate_ref_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str | None,
    oldrev: str,
    newrev: str,
    refname: str,
    paths: list[str] | None = None,
) -> tuple[dict, list[tuple[dict, dict]], list[str]]:
    """Apply every push rule to one ref update without changing any state.

    Returns (repository, [(approval, operation)] the update needs, paths).
    Raises PushRejected. Runs in pre-receive and again at the reference
    transaction's `prepared` stage, against the state current while Git holds
    the ref locks. The second run passes the paths pre-receive derived, so
    the operations (and their hashes) are the ones that were approved even
    when an earlier ref of the same push has since made its commits known.
    """
    repo = _verify_repository_binding(conn, repository, git_dir)
    refusal = ref_namespace_refusal(repo, refname)
    if refusal is not None:
        raise PushRejected(refusal)
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

    try:
        if paths is None:
            paths = git_objects.changed_paths(
                git_dir, oldrev, newrev, git_objects.ref_tips(git_dir)
            )
        operations = governed_operations(
            conn, repo, refname, oldrev, newrev, git_dir, paths
        )
    except git_objects.GitObjectError as exc:
        raise PushRejected(f"Unable to inspect pushed objects: {exc}") from exc

    granted: list[tuple[dict, dict]] = []
    missing: list[str] = []
    for operation in operations:
        approval = approved_instance(conn, operation, consumer=pusher)
        if approval is None:
            missing.append(_requirement_message(
                operation, approval_refusal(conn, operation, consumer=pusher)
            ))
        else:
            granted.append((approval, operation))
    if missing:
        if len(operations) > 1:
            missing.append(
                "Run `quorumgit approve prepare` from your clone to list "
                "and request every approval this push needs."
            )
        raise PushRejected(" ".join(missing))
    return repo, granted, paths


def check_ref_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str,
    oldrev: str,
    newrev: str,
    refname: str,
) -> None:
    """pre-receive: validate one ref update and record it for its transaction.

    Nothing is consumed here. Git may still refuse the update after
    pre-receive (another hook, a lost ref lock), so approvals are spent only
    once Git has locked the ref, in the reference-transaction hook.
    """
    repo, granted, paths = evaluate_ref_update(
        conn, repository, git_dir, pusher, oldrev, newrev, refname
    )
    operations = [operation for _, operation in granted]
    row = conn.execute(
        """
        INSERT INTO ref_updates (
            repository_id, refname, oldrev, newrev, pusher_agent_id,
            paths, operations
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            repo["id"],
            refname,
            oldrev,
            newrev,
            get_agent(conn, pusher)["id"],
            json_dumps(paths),
            json_dumps(operations),
        ),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        "gate.update_validated",
        "ref_update",
        row[0],
        agent=pusher,
        detail={
            "refname": refname,
            "oldrev": oldrev,
            "newrev": newrev,
            "paths": len(paths),
            "operations": operations,
            "approval_ids": [approval["id"] for approval, _ in granted],
        },
    )


# ---------------------------------------------------------- push planning


def _full_refname(ref: str) -> str:
    return ref if ref.startswith("refs/") else f"refs/heads/{ref}"


def prepare_push(
    conn: Connection,
    repository: str,
    ref: str,
    local_path: str | Path,
    new: str | None = "HEAD",
    *,
    pusher: str | None = None,
) -> dict:
    """What pushing ``new`` to ``ref`` would require, without pushing anything.

    Objects are read from the agent's clone at ``local_path`` and ref state
    from the registered repository, and the same derivation the hook uses
    produces the operations, so their hashes match what the hook will
    demand. ``new=None`` plans a deletion. Nothing is written.
    """
    repo = get_repository(conn, repository)
    refname = _full_refname(ref)
    hub_dir = git_common_dir(repo["path"])
    local_dir = git_objects.absolute_git_dir(local_path)
    oldrev = git_objects.ref_value(hub_dir, refname)
    if new is None:
        if is_zero(oldrev):
            raise GateError(f"{refname} does not exist in {repository!r}.")
        newrev = zero_oid_like(oldrev)
    else:
        newrev = git_objects.resolve_object(local_dir, new)
        if is_zero(oldrev):
            oldrev = zero_oid_like(newrev)
    if not is_zero(oldrev) and not git_objects.object_exists(local_dir, oldrev):
        raise GateError(
            f"{refname} is at {oldrev} in {repository!r}, which your clone does "
            "not have; fetch before preparing this push."
        )
    known = git_objects.ref_tips(hub_dir)
    missing = [
        tip for tip in known if not git_objects.object_exists(local_dir, tip)
    ]
    if missing and not is_zero(newrev):
        # Commits reachable from a tip the clone lacks would look new here but
        # not to the hook, so the derived paths (and hashes) would differ.
        raise GateError(
            f"Your clone lacks {len(missing)} commit(s) that refs in "
            f"{repository!r} point at (for example {missing[0]}); fetch every "
            "ref you can push to before preparing this push, so the plan "
            "matches what the hook will see."
        )
    paths = git_objects.changed_paths(local_dir, oldrev, newrev, known)

    refusals: list[str] = []
    namespace = ref_namespace_refusal(repo, refname)
    if namespace is not None:
        refusals.append(namespace)
    claim = None
    if refname.startswith("refs/heads/"):
        branch = refname.removeprefix("refs/heads/")
        pending = open_handoff_for_branch(conn, repo["id"], branch)
        if pending:
            refusals.append(
                f"Branch {branch!r} is frozen pending handoff {pending['id']}."
            )
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim is not None:
            claim = {**claim, "scopes": claim_scopes(conn, claim["id"])}
            if pusher is not None and claim["agent"] != pusher:
                refusals.append(
                    f"Branch {branch!r} is claimed by {claim['agent']} "
                    f"(claim {claim['id']}), not {pusher}."
                )
    operations = governed_operations(
        conn, repo, refname, oldrev, newrev, local_dir, paths
    )
    return {
        "repository": repo["name"],
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
        "paths": paths,
        "claim": claim,
        "refusals": refusals,
        "operations": [
            {"operation": op, "hash": operation_hash(op)} for op in operations
        ],
    }


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
        # Without the transaction hook nothing would re-validate at update
        # time or consume approvals, so a missing one must reject the push.
        _require_reference_transaction_hook(conn, repository)
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        conn.rollback()
        print(f"[quorumgit] REJECTED: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    print("[quorumgit] accepted.")
    return 0


# ------------------------------------------------------ reference transaction

TRANSACTION_STATES = ("prepared", "committed", "aborted")
# pre-receive and the reference transaction run seconds apart. A validation
# older than this is stale (Git refused the update after pre-receive) and is
# never matched against a later transaction.
VALIDATION_TTL_SECONDS = 300
# A prepared update normally resolves within milliseconds, when Git commits or
# aborts the locked transaction. One older than this lost its outcome hook
# (killed process, unreachable store) and doctor reconciles it from the ref.
PREPARED_STUCK_AFTER_SECONDS = VALIDATION_TTL_SECONDS


def _parse_updates(stdin_lines: Iterable[str]) -> list[tuple[str, str, str]]:
    updates = []
    for line in stdin_lines:
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 3:
            raise PushRejected(f"Malformed reference-transaction input: {line!r}")
        updates.append((parts[0], parts[1], parts[2]))
    return updates


def _find_update(
    conn: Connection,
    repository_id: int,
    status: str,
    oldrev: str,
    newrev: str,
    refname: str,
) -> dict | None:
    """The newest recorded update matching one transaction line.

    Git reports the zero OID as the old value when a transaction does not
    check it, so a zero old value matches any recorded one.
    """
    row = conn.execute(
        """
        SELECT u.id, u.oldrev, a.name, u.paths, u.approval_ids
        FROM ref_updates u JOIN agents a ON a.id = u.pusher_agent_id
        WHERE u.repository_id = ? AND u.status = ? AND u.refname = ?
          AND u.newrev = ? AND (u.oldrev = ? OR ?)
          AND u.created_at >= unixepoch() - ?
        ORDER BY u.id DESC
        LIMIT 1
        """,
        (
            repository_id,
            status,
            refname,
            newrev,
            oldrev,
            1 if is_zero(oldrev) else 0,
            VALIDATION_TTL_SECONDS,
        ),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "oldrev": row[1],
        "pusher": row[2],
        "paths": json_loads(row[3], []),
        "approval_ids": json_loads(row[4], []),
    }


def _prepare_updates(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str | None,
    updates: list[tuple[str, str, str]],
) -> None:
    """Re-validate and authorize validated updates while Git holds the locks.

    Only an unidentified transaction with no matching validation is local ref
    maintenance. An identified one (every push carries QUORUMGIT_AGENT, which
    pre-receive requires) must match a fresh validation, or it is rejected:
    an expired or missing record never downgrades a push to pass-through.
    """
    repo = get_repository(conn, repository)
    governed = []
    # Classify without the write reservation, so pass-through maintenance
    # never waits on (or deadlocks with) a command holding the store.
    for oldrev, newrev, refname in updates:
        if not refname.startswith("refs/"):
            # A symbolic ref such as HEAD is listed alongside the ref it
            # points to; that ref appears as its own line and is governed
            # there. Pushes only ever name refs under refs/.
            continue
        if _find_update(conn, repo["id"], "validated", oldrev, newrev, refname):
            governed.append((oldrev, newrev, refname))
            continue
        if pusher is not None:
            raise PushRejected(
                f"Update of {refname} to {newrev} by {pusher} has no current "
                "pre-receive validation; push it again."
            )
        stray = conn.execute(
            "SELECT id FROM ref_updates WHERE repository_id = ? "
            "AND refname = ? AND status = 'validated' "
            "AND created_at >= unixepoch() - ? LIMIT 1",
            (repo["id"], refname, VALIDATION_TTL_SECONDS),
        ).fetchone()
        if stray is not None:
            raise PushRejected(
                f"Update of {refname} to {newrev} does not match the update "
                "pre-receive validated."
            )
    if not governed:
        return
    begin_immediate(conn)
    for oldrev, newrev, refname in governed:
        found = _find_update(conn, repo["id"], "validated", oldrev, newrev, refname)
        if found is None:
            raise PushRejected(f"Update of {refname} changed while being prepared.")
        if pusher != found["pusher"]:
            raise PushRejected(
                f"Update of {refname} was validated for {found['pusher']}, "
                f"not {pusher or 'an unidentified pusher'}."
            )
        _, granted, _ = evaluate_ref_update(
            conn,
            repository,
            git_dir,
            pusher,
            found["oldrev"],
            newrev,
            refname,
            paths=found["paths"],
        )
        assert pusher is not None
        for approval, operation in granted:
            consume_approval(conn, approval["id"], operation, agent=pusher)
        approval_ids = [approval["id"] for approval, _ in granted]
        cur = conn.execute(
            "UPDATE ref_updates SET status = 'prepared', approval_ids = ? "
            "WHERE id = ? AND status = 'validated'",
            (json_dumps(approval_ids), found["id"]),
        )
        if cur.rowcount != 1:
            raise PushRejected(f"Update of {refname} changed while being prepared.")
        audit.record(
            conn,
            "gate.update_prepared",
            "ref_update",
            found["id"],
            agent=pusher,
            detail={
                "refname": refname,
                "oldrev": found["oldrev"],
                "newrev": newrev,
                "approval_ids": approval_ids,
            },
        )


def _restore_approval(conn: Connection, update_id: int, approval_id: int) -> None:
    """Return an approval consumed for an update Git then aborted."""
    try:
        cur = conn.execute(
            "UPDATE approvals SET status = 'approved', consumed_at = NULL, "
            "consumed_by_agent_id = NULL WHERE id = ? AND status = 'consumed'",
            (approval_id,),
        )
    except sqlite3.IntegrityError as exc:
        # A newer live instance for the same operation now exists; the old
        # one stays consumed rather than creating two live approvals.
        audit.record(
            conn,
            "approval.restore_skipped",
            "approval",
            approval_id,
            detail={"ref_update_id": update_id, "reason": str(exc)},
        )
        return
    if cur.rowcount == 1:
        audit.record(
            conn,
            "approval.restored",
            "approval",
            approval_id,
            detail={"ref_update_id": update_id},
        )


def _resolve_updates(
    conn: Connection,
    repository: str,
    state: str,
    updates: list[tuple[str, str, str]],
) -> None:
    begin_immediate(conn)
    repo = get_repository(conn, repository)
    statuses = ("prepared",) if state == "committed" else ("prepared", "validated")
    for oldrev, newrev, refname in updates:
        for status in statuses:
            found = _find_update(conn, repo["id"], status, oldrev, newrev, refname)
            if found is None:
                continue
            conn.execute(
                "UPDATE ref_updates SET status = ?, resolved_at = unixepoch() "
                "WHERE id = ? AND status = ?",
                (state, found["id"], status),
            )
            if state == "aborted":
                for approval_id in found["approval_ids"]:
                    _restore_approval(conn, found["id"], approval_id)
            audit.record(
                conn,
                f"gate.update_{state}",
                "ref_update",
                found["id"],
                agent=found["pusher"],
                detail={"refname": refname, "newrev": newrev},
            )
            break


def run_reference_transaction(
    conn: Connection, repository: str, state: str, stdin_lines: Iterable[str]
) -> int:
    """Hook entry point for Git's reference-transaction hook.

    `prepared` is the authoritative gate: its failure makes Git abort the
    transaction, so it fails closed. Git ignores the exit status of
    `committed` and `aborted`; they only record the outcome.
    """
    if state not in TRANSACTION_STATES:
        return 0
    git_dir = os.environ.get("GIT_DIR") or "."
    pusher = os.environ.get("QUORUMGIT_AGENT") or None
    try:
        updates = _parse_updates(stdin_lines)
        if state == "prepared":
            _prepare_updates(conn, repository, git_dir, pusher, updates)
        else:
            _resolve_updates(conn, repository, state, updates)
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        conn.rollback()
        label = "REJECTED" if state == "prepared" else f"WARNING ({state})"
        print(f"[quorumgit] {label}: {exc}", file=sys.stderr)
        return 1
    conn.commit()
    return 0


# ------------------------------------------------- doctor: stuck ref updates


def _current_ref(repository_path: str, refname: str) -> str | None:
    """The ref's object ID in the repository, or None when it does not exist."""
    result = subprocess.run(
        ["git", "-C", repository_path, "rev-parse", "--verify", "--quiet", refname],
        capture_output=True,
        text=True,
        env=path_scoped_git_env(),
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip().lower()
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise GateError(f"Unable to read {refname} in {repository_path}: {detail}")


def _ref_is_locked(repository_path: str, refname: str) -> bool:
    """True while Git holds the files-backend lock for the ref."""
    result = subprocess.run(
        ["git", "-C", repository_path, "rev-parse", "--git-path", f"{refname}.lock"],
        capture_output=True,
        text=True,
        env=path_scoped_git_env(),
        check=False,
    )
    if result.returncode != 0:
        return False
    lock = Path(result.stdout.strip())
    if not lock.is_absolute():
        lock = Path(repository_path) / lock
    return lock.exists()


def doctor_ref_updates(conn: Connection, repair: bool = False) -> list[dict]:
    """Report and conservatively reconcile ref updates stuck in `prepared`.

    The outcome is read from the ref itself: at the new value (or gone, for
    a deletion) means Git committed; still at the old value (or absent, for a
    creation) means Git aborted, so the consumed approvals are restored. A ref
    that has since moved elsewhere, or is still locked, is reported for manual
    inspection and never guessed at.
    """
    if repair:
        begin_immediate(conn)
    rows = conn.execute(
        """
        SELECT u.id, u.refname, u.oldrev, u.newrev, u.approval_ids, r.name, r.path
        FROM ref_updates u JOIN repositories r ON r.id = u.repository_id
        WHERE u.status = 'prepared' AND u.created_at < unixepoch() - ?
        ORDER BY u.id
        """,
        (PREPARED_STUCK_AFTER_SECONDS,),
    ).fetchall()
    findings: list[dict] = []
    for update_id, refname, oldrev, newrev, approval_ids, repo_name, repo_path in rows:
        finding: dict[str, Any] = {
            "ref_update_id": update_id,
            "repository": repo_name,
            "refname": refname,
            "issue": "stuck_prepared",
            "outcome": None,
            "repaired": False,
        }
        findings.append(finding)
        try:
            if _ref_is_locked(repo_path, refname):
                finding["error"] = "ref is still locked by Git; retry once it is released"
                continue
            current = _current_ref(repo_path, refname)
        except GateError as exc:
            finding["error"] = str(exc)
            continue
        new_value = None if is_zero(newrev) else newrev
        old_value = None if is_zero(oldrev) else oldrev
        if current == new_value:
            finding["outcome"] = "committed"
        elif current == old_value:
            finding["outcome"] = "aborted"
        else:
            finding["error"] = (
                f"{refname} has since moved to {current or 'deletion'}; the outcome "
                "cannot be proven from the ref. Inspect it manually."
            )
            continue
        if not repair:
            continue
        cur = conn.execute(
            "UPDATE ref_updates SET status = ?, resolved_at = unixepoch() "
            "WHERE id = ? AND status = 'prepared'",
            (finding["outcome"], update_id),
        )
        if cur.rowcount != 1:
            finding["error"] = "ref update changed while being reconciled"
            continue
        if finding["outcome"] == "aborted":
            for approval_id in json_loads(approval_ids, []):
                _restore_approval(conn, update_id, approval_id)
        audit.record(
            conn,
            f"gate.update_reconciled_{finding['outcome']}",
            "ref_update",
            update_id,
            detail={"refname": refname, "current": current},
        )
        finding["repaired"] = True
    return findings


# ------------------------------------------------------------ hook install

HOOK_NAMES = ("pre-receive", "reference-transaction")
REFERENCE_TRANSACTION_MARKER = "# quorumgit-managed-reference-transaction v1"
# The reference-transaction hook (with its prepared/committed/aborted states).
MIN_GIT_VERSION = (2, 28)


def _git_version() -> tuple[int, ...]:
    result = subprocess.run(
        ["git", "version"], capture_output=True, text=True, check=False
    )
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if result.returncode != 0 or match is None:
        raise GateError("Unable to determine the Git version.")
    return (int(match.group(1)), int(match.group(2)))


def _require_git_version() -> None:
    version = _git_version()
    if version < MIN_GIT_VERSION:
        raise GateError(
            f"Git {'.'.join(map(str, version))} lacks the reference-transaction "
            f"hook; QuorumGit needs Git {'.'.join(map(str, MIN_GIT_VERSION))} or newer."
        )


def _effective_hook(repository_path: str | Path, name: str) -> Path:
    repo_path = Path(repository_path).resolve()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-path", f"hooks/{name}"],
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
        raise GateError(f"Git returned no {name} hook path for {repo_path}")
    hook_path = Path(raw)
    if not hook_path.is_absolute():
        hook_path = repo_path / hook_path
    return hook_path.resolve()


def _effective_pre_receive_hook(repository_path: str | Path) -> Path:
    return _effective_hook(repository_path, "pre-receive")


def _hook_script(repository: str, executable: str | None = None) -> str:
    python = executable or sys.executable
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        f"exec {shlex.quote(python)} -m quorumgit hook pre-receive "
        f"--repo {shlex.quote(repository)}\n"
    )


def _reference_transaction_script(
    repository: str, executable: str | None = None
) -> str:
    python = executable or sys.executable
    return (
        "#!/bin/sh\n"
        f"{REFERENCE_TRANSACTION_MARKER}\n"
        'case "$1" in\n'
        "prepared|committed|aborted) ;;\n"
        "*) cat >/dev/null; exit 0 ;;\n"
        "esac\n"
        f"exec {shlex.quote(python)} -m quorumgit hook reference-transaction "
        f'--repo {shlex.quote(repository)} "$1"\n'
    )


def _legacy_hook_script(repository: str) -> str:
    """Exact pre-PR6 hook shape, recognized only for safe in-place upgrade."""
    return (
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -m quorumgit hook pre-receive '
        f'--repo "{repository}"\n'
    )


def _hook_plan(repository: str, name: str) -> tuple[str, str, tuple[str, ...]]:
    """(expected content, ownership marker, upgradeable legacy contents)."""
    if name == "pre-receive":
        return _hook_script(repository), HOOK_MARKER, (_legacy_hook_script(repository),)
    return _reference_transaction_script(repository), REFERENCE_TRANSACTION_MARKER, ()


def _require_reference_transaction_hook(conn: Connection, repository: str) -> None:
    _require_git_version()
    repo = get_repository(conn, repository)
    hook_path = _effective_hook(repo["path"], "reference-transaction")
    expected, _, _ = _hook_plan(repository, "reference-transaction")
    try:
        installed = hook_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        installed = None
    if installed != expected:
        raise PushRejected(
            f"The QuorumGit reference-transaction hook is missing or modified at "
            f"{hook_path}; run `quorumgit hook install --repo {repository}`."
        )
    # Git silently ignores a hook that is not an executable regular file.
    if not hook_path.is_file() or not os.access(hook_path, os.X_OK):
        raise PushRejected(
            f"The QuorumGit reference-transaction hook at {hook_path} is not "
            f"executable, so Git would skip it; run `quorumgit hook install "
            f"--repo {repository}`."
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


def _inspect_hook(
    hook_path: Path, expected: str, marker: str, legacy: tuple[str, ...]
) -> str:
    """Decide what installing over `hook_path` means; refuse foreign content."""
    if not hook_path.exists():
        return "installed"
    try:
        existing = hook_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GateError(f"Cannot inspect existing hook {hook_path}: {exc}") from exc
    if existing == expected:
        return "verified"
    if existing in legacy:
        return "upgraded"
    if marker in existing.splitlines()[:3]:
        raise GateError(
            f"Existing QuorumGit-managed hook differs at {hook_path}; "
            "refusing to overwrite it."
        )
    raise GateError(
        f"Existing hook at {hook_path} is not owned by QuorumGit; refusing to "
        "overwrite or silently chain it."
    )


def install_hook(conn: Connection, repository: str) -> Path:
    """Safely install QuorumGit's pre-receive and reference-transaction hooks.

    Both go at Git's effective hook paths. Existing unrelated hooks are never
    overwritten or silently chained, and every existing hook is inspected
    before anything is written, so a refusal leaves both paths untouched. An
    exact current QuorumGit hook is idempotent; an exact legacy QuorumGit hook
    for the same repository is upgraded in place. Returns the pre-receive path.
    """
    begin_immediate(conn)
    _require_git_version()
    repo = get_repository(conn, repository)
    common_dir = assert_repository_identity_unique(conn, repo)

    plans = []
    for name in HOOK_NAMES:
        expected, marker, legacy = _hook_plan(repository, name)
        hook_path = _effective_hook(repo["path"], name)
        plans.append(
            (name, hook_path, expected, _inspect_hook(hook_path, expected, marker, legacy))
        )

    for name, hook_path, expected, action in plans:
        if action == "verified":
            hook_path.chmod(0o755)
        else:
            _write_hook_atomically(hook_path, expected)
        effective = _effective_hook(repo["path"], name)
        if effective != hook_path or not hook_path.exists():
            raise GateError(f"Installed hook is not Git's effective {name} hook: {hook_path}")
        if hook_path.read_text(encoding="utf-8") != expected:
            raise GateError(f"Installed {name} hook failed verification: {hook_path}")
        audit.record(
            conn,
            f"hook.{action}",
            "repository",
            repo["id"],
            detail={
                "hook": name,
                "path": str(hook_path),
                "git_common_dir": str(common_dir),
            },
        )
    return plans[0][1]
