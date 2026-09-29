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
import shlex
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
    scope_paths: list[str],
) -> list[dict]:
    """Every approval one ref update needs, derived from what it carries.

    ``paths`` are the paths the update brings into the repository, measured
    against every existing ref; they are checked against protected paths.
    ``scope_paths`` are measured against the mainline only
    (git_objects.mainline_tips), so work first pushed to another branch still
    counts against the claim that brings it in. Ref-level governance (protected ref, force push, deletion) comes first,
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

    if refname.startswith("refs/heads/") and scope_paths:
        branch = refname.removeprefix("refs/heads/")
        claim = live_claim_for_branch(conn, repo["id"], branch)
        if claim is not None:
            outside = paths_outside(scope_paths, claim_scopes(conn, claim["id"]))
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
        paths = git_objects.changed_paths(
            git_dir, oldrev, newrev, git_objects.ref_tips(git_dir)
        )
        scope_paths = git_objects.changed_paths(
            git_dir,
            oldrev,
            newrev,
            git_objects.mainline_tips(git_dir, repo["protected_refs"]),
        )
        operations = governed_operations(
            conn, repo, refname, oldrev, newrev, git_dir, paths, scope_paths
        )
    except git_objects.GitObjectError as exc:
        raise PushRejected(f"Unable to inspect pushed objects: {exc}") from exc

    if operations:
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
        assert pusher is not None
        for approval, operation in granted:
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
        detail={
            "refname": refname,
            "oldrev": oldrev,
            "newrev": newrev,
            "paths": len(paths),
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
    # Mainline tips are hub ref tips, so the check above covers them too.
    scope_paths = git_objects.changed_paths(
        local_dir,
        oldrev,
        newrev,
        git_objects.mainline_tips(hub_dir, repo["protected_refs"]),
    )

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
        conn, repo, refname, oldrev, newrev, local_dir, paths, scope_paths
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
