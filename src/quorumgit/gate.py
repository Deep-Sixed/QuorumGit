"""Approval gate and pre-receive enforcement.

Protected operations require an approval whose hash binds to the exact
operation payload. Enforcement is fail-closed: any hook error rejects the push.
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

from . import audit
from .canonical import stable_hash
from .registry import (
    RegistryError,
    assert_repository_identity_unique,
    get_agent,
    get_repository,
    path_scoped_git_env,
)
from .store import Connection, begin_immediate, json_dumps, json_loads
from .work import (
    claim_scopes,
    live_claim_for_branch,
    open_handoff_for_branch,
    path_in_scopes,
)

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


def _beneficiary(operation: dict[str, Any]) -> str | None:
    """The agent an operation authorizes, when it is known before consumption."""
    if operation.get("type") == "lease_takeover":
        return str(operation.get("to_agent") or "") or None
    return None


def _independent_yes_votes(
    conn: Connection, approval_id: int, excluding_agent_id: int
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM votes "
        "WHERE approval_id = ? AND vote = 1 AND voter_agent_id <> ?",
        (approval_id, excluding_agent_id),
    ).fetchone()
    assert row is not None
    return row[0]


def vote(conn: Connection, approval_id: int, voter: str, approve: bool) -> dict:
    """Record a vote against one explicit approval instance atomically.

    BEGIN IMMEDIATE serializes competing voters before either reads the current
    approval state. Denial has precedence and denied/consumed are terminal.
    An approved instance still accepts votes until it is consumed: further yes
    votes let independent approvers replace a self-vote that consumption will
    not count, and a no vote revokes it before use. An agent may never approve
    an operation whose known beneficiary is itself.
    """
    begin_immediate(conn)
    voter_row = get_agent(conn, voter)
    approval = get_approval_by_id(conn, approval_id)
    if approval["status"] not in ("pending", "approved"):
        raise GateError(f"Approval {approval_id} is already {approval['status']}.")
    if approve and _beneficiary(approval["operation"]) == voter:
        raise GateError(
            f"Agent {voter} cannot approve operation {approval_id} that "
            "authorizes itself; another registered agent must approve it."
        )
    threshold = approval["threshold"]
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

    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE vote = 1), "
        "count(*) FILTER (WHERE vote = 0) "
        "FROM votes WHERE approval_id = ?",
        (approval["id"],),
    ).fetchone()
    assert counts is not None
    yes, no = counts
    if no > 0:
        new_status = "denied"
    elif yes >= threshold:
        new_status = "approved"
    else:
        new_status = "pending"
    if new_status != approval["status"]:
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = unixepoch() "
            "WHERE id = ? AND status = ?",
            (new_status, approval["id"], approval["status"]),
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
    independent = _independent_yes_votes(conn, approval["id"], consumer["id"])
    if independent < approval["threshold"]:
        raise GateError(
            f"Approval {approval_id} needs {approval['threshold']} approving "
            f"vote(s) from agents other than {agent}, who is using it; it has "
            f"{independent}. An agent cannot authorize its own operation."
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


MAX_REPORTED_PATHS = 10


def _pushed_paths(git_dir: str, newrev: str) -> list[str]:
    """Paths changed by the commits this push introduces to the repository.

    `newrev --not --all` is exactly the set of new commits: in pre-receive no
    ref points at them yet. `--cc` lists only the paths a merge commit itself
    authored (differing from every parent), so merging the base branch in does
    not attribute the base's files to the pusher. Renames count as both paths.
    """
    result = subprocess.run(
        [
            "git",
            "--git-dir",
            git_dir,
            "log",
            "--format=",
            "--name-only",
            "--no-renames",
            "--cc",
            "-z",
            newrev,
            "--not",
            "--all",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PushRejected("Unable to determine the paths changed by this push.")
    paths = (
        entry.lstrip("\n")
        for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0")
    )
    return list(dict.fromkeys(path for path in paths if path))


def enforce_claim_scopes(
    conn: Connection, git_dir: str, claim: dict, branch: str, newrev: str
) -> None:
    """Reject new commits that touch paths outside the claim's declared scopes."""
    scopes = claim_scopes(conn, claim["id"])
    outside = [
        path for path in _pushed_paths(git_dir, newrev)
        if not path_in_scopes(path, scopes)
    ]
    if outside:
        shown = ", ".join(outside[:MAX_REPORTED_PATHS])
        more = len(outside) - MAX_REPORTED_PATHS
        if more > 0:
            shown += f", and {more} more"
        raise PushRejected(
            f"Push to {branch!r} changes paths outside claim {claim['id']}'s "
            f"scopes {scopes}: {shown}. Claim a scope that covers them first."
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


def _required_approval(
    conn: Connection, operation: dict[str, Any], pusher: str
) -> dict:
    """The approval that authorizes `operation` for `pusher`, or PushRejected."""
    approval = approved_instance(conn, operation)
    if approval is None:
        raise PushRejected(
            f"{operation['type']} on {operation['refname']} requires an approval "
            f"bound to this exact update (hash {operation_hash(operation)})."
        )
    independent = _independent_yes_votes(
        conn, approval["id"], get_agent(conn, pusher)["id"]
    )
    if independent < approval["threshold"]:
        raise PushRejected(
            f"Approval {approval['id']} needs {approval['threshold']} approving "
            f"vote(s) from agents other than {pusher}, who is pushing; it has "
            f"{independent}. An agent cannot authorize its own operation."
        )
    return approval


def evaluate_ref_update(
    conn: Connection,
    repository: str,
    git_dir: str,
    pusher: str | None,
    oldrev: str,
    newrev: str,
    refname: str,
) -> tuple[dict, dict[str, Any] | None, dict | None]:
    """Apply every push rule to one ref update without changing any state.

    Returns (repository, protected operation or None, its approval or None).
    Raises PushRejected. Runs twice per governed update: in pre-receive, and
    again at the reference transaction's `prepared` stage against the state
    current while Git holds the ref locks.
    """
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
        if claim and not _is_zero(newrev):
            enforce_claim_scopes(conn, git_dir, claim, branch, newrev)

    protected = refname in repo["protected_refs"]
    deletion = _is_zero(newrev)
    forced = (
        not deletion
        and not _is_zero(oldrev)
        and not _is_fast_forward(git_dir, oldrev, newrev)
    )
    if not (protected or deletion or forced):
        return repo, None, None

    operation = {
        "type": "protected_ref_update"
        if protected
        else ("ref_delete" if deletion else "force_update"),
        "repository": repository,
        "refname": refname,
        "oldrev": oldrev,
        "newrev": newrev,
    }
    if pusher is None:
        raise PushRejected("Pusher identity required for a protected update.")
    return repo, operation, _required_approval(conn, operation, pusher)


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
    pre-receive (another hook, a lost ref lock), so the approval is only spent
    once Git has locked the ref, in the reference-transaction hook.
    """
    repo, operation, approval = evaluate_ref_update(
        conn, repository, git_dir, pusher, oldrev, newrev, refname
    )
    row = conn.execute(
        """
        INSERT INTO ref_updates (
            repository_id, refname, oldrev, newrev, pusher_agent_id, operation
        )
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            repo["id"],
            refname,
            oldrev,
            newrev,
            get_agent(conn, pusher)["id"],
            json_dumps(operation) if operation else None,
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
            "operation": operation,
            "approval_id": approval["id"] if approval else None,
        },
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
) -> tuple | None:
    """The newest recorded update matching one transaction line.

    Git reports the zero OID as the old value when a transaction does not
    check it, so a zero old value matches any recorded one.
    """
    return conn.execute(
        """
        SELECT u.id, u.oldrev, a.name, u.approval_id
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
            1 if _is_zero(oldrev) else 0,
            VALIDATION_TTL_SECONDS,
        ),
    ).fetchone()


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
        found = _find_update(conn, repo["id"], "validated", oldrev, newrev, refname)
        if found is not None:
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
        update_id, validated_old, validated_pusher, _ = found
        if pusher != validated_pusher:
            raise PushRejected(
                f"Update of {refname} was validated for {validated_pusher}, "
                f"not {pusher or 'an unidentified pusher'}."
            )
        _, operation, approval = evaluate_ref_update(
            conn, repository, git_dir, pusher, validated_old, newrev, refname
        )
        if operation is not None:
            assert approval is not None and pusher is not None
            consume_approval(conn, approval["id"], operation, agent=pusher)
        cur = conn.execute(
            "UPDATE ref_updates SET status = 'prepared', operation = ?, "
            "approval_id = ? WHERE id = ? AND status = 'validated'",
            (
                json_dumps(operation) if operation else None,
                approval["id"] if approval else None,
                update_id,
            ),
        )
        if cur.rowcount != 1:
            raise PushRejected(f"Update of {refname} changed while being prepared.")
        audit.record(
            conn,
            "gate.update_prepared",
            "ref_update",
            update_id,
            agent=pusher,
            detail={
                "refname": refname,
                "oldrev": validated_old,
                "newrev": newrev,
                "approval_id": approval["id"] if approval else None,
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
    for oldrev, newrev, refname in updates:
        statuses = ("prepared",) if state == "committed" else ("prepared", "validated")
        for status in statuses:
            found = _find_update(conn, repo["id"], status, oldrev, newrev, refname)
            if found is None:
                continue
            update_id, _, pusher, approval_id = found
            conn.execute(
                "UPDATE ref_updates SET status = ?, resolved_at = unixepoch() "
                "WHERE id = ? AND status = ?",
                (state, update_id, status),
            )
            if state == "aborted" and approval_id is not None:
                _restore_approval(conn, update_id, approval_id)
            audit.record(
                conn,
                f"gate.update_{state}",
                "ref_update",
                update_id,
                agent=pusher,
                detail={"refname": refname, "newrev": newrev},
            )
            break


# A prepared update normally resolves within milliseconds, when Git commits or
# aborts the locked transaction. One older than this lost its outcome hook
# (killed process, unreachable store) and is reconciled from the ref itself.
PREPARED_STUCK_AFTER_SECONDS = VALIDATION_TTL_SECONDS


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
    creation) means Git aborted, so the consumed approval is restored. A ref
    that has since moved elsewhere, or is still locked, is reported for
    manual inspection and never guessed at.
    """
    if repair:
        begin_immediate(conn)
    rows = conn.execute(
        """
        SELECT u.id, u.refname, u.oldrev, u.newrev, u.approval_id, r.name, r.path
        FROM ref_updates u JOIN repositories r ON r.id = u.repository_id
        WHERE u.status = 'prepared' AND u.created_at < unixepoch() - ?
        ORDER BY u.id
        """,
        (PREPARED_STUCK_AFTER_SECONDS,),
    ).fetchall()
    findings: list[dict] = []
    for update_id, refname, oldrev, newrev, approval_id, repo_name, repo_path in rows:
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
        new_value = None if _is_zero(newrev) else newrev
        old_value = None if _is_zero(oldrev) else oldrev
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
        if finding["outcome"] == "aborted" and approval_id is not None:
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


def _inspect_hook(hook_path: Path, expected: str, marker: str, legacy: tuple[str, ...]) -> str:
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
