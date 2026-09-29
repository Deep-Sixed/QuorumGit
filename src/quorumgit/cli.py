"""quorumgit command-line interface.

Exit codes: 0 success, 1 refused/violation/error, 2 usage error.
Agent identity comes from --agent or the QUORUMGIT_AGENT environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    __version__,
    audit,
    config,
    gate,
    git_objects,
    handoff,
    registry,
    store,
    trees,
    work,
)


def _agent(args, cfg) -> str:
    agent = getattr(args, "agent", None) or cfg.agent
    if not agent:
        raise SystemExit(
            "Agent identity required: pass --agent or set QUORUMGIT_AGENT."
        )
    return agent


# ------------------------------------------------------------ store commands


def cmd_init(args, cfg) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    uri = store.ensure_running(cfg)
    store.provision_extensions(uri)
    applied = store.migrate(uri)
    store.verify_contract(uri)
    print(f"store: {uri}")
    print(f"migrations applied: {applied or 'none (up to date)'}")
    print("contract: ok")
    return 0


def cmd_status(args, cfg) -> int:
    info = store.instance_status(cfg)

    if info.get("running") is False:
        print("running: false")
        return 1
    uri = info["uri"]
    print(f"uri: {uri}")
    try:
        store.verify_contract(uri)
        print("contract: ok")
    except store.StoreError as exc:
        print(f"contract: VIOLATED — {exc}")
        return 1
    with store.session(cfg) as conn:
        for table in ("repositories", "agents", "tasks", "claims", "handoffs"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            print(f"{table}: {row[0]}")
    return 0


def cmd_destroy(args, cfg) -> int:
    if not args.yes:
        print("Refusing to destroy without --yes.", file=sys.stderr)
        return 1
    store.destroy(cfg)
    print("store destroyed.")
    return 0


def cmd_doctor(args, cfg) -> int:
    """Inspect and optionally reconcile worktree drift and stuck ref updates."""
    with store.session(cfg) as conn:
        findings = trees.doctor_worktrees(conn, repair=args.repair)
        ref_findings = gate.doctor_ref_updates(conn, repair=args.repair)
        if args.repair:
            conn.commit()
    unresolved = False

    def state_of(finding: dict) -> str:
        nonlocal unresolved
        if finding.get("error"):
            unresolved = True
            prefix = "repair failed" if args.repair else "unresolvable"
            return f"{prefix}: {finding['error']}"
        if finding.get("repaired"):
            return "repaired"
        unresolved = True
        return "detected"

    for finding in findings:
        print(
            f"worktree {finding['worktree_id']} claim {finding['claim_id']}: "
            f"{finding['issue']} — {state_of(finding)} — {finding['path']}"
        )
    for finding in ref_findings:
        outcome = f" (git {finding['outcome']})" if finding.get("outcome") else ""
        print(
            f"ref update {finding['ref_update_id']} {finding['repository']} "
            f"{finding['refname']}: {finding['issue']}{outcome} — {state_of(finding)}"
        )
    if not findings:
        print("worktrees: ok")
    if not ref_findings:
        print("ref updates: ok")
    if not unresolved:
        if args.repair and (findings or ref_findings):
            print("reconciled.")
        return 0
    if not args.repair:
        print("re-run with `quorumgit doctor --repair` to reconcile safe cases.")
    return 1


# --------------------------------------------------------- registry commands


def cmd_repo_add(args, cfg) -> int:
    with store.session(cfg) as conn:
        repo_id = registry.add_repository(
            conn,
            args.name,
            args.path,
            protected_refs=args.protected_ref,
            protected_paths=args.protected_path,
        )
        conn.commit()
    print(f"repository {args.name} registered (id {repo_id}).")
    return 0


def cmd_repo_list(args, cfg) -> int:
    with store.session(cfg) as conn:
        for repo in registry.list_repositories(conn):
            refs = ",".join(repo["protected_refs"]) or "-"
            print(f"{repo['id']}\t{repo['name']}\t{repo['path']}\tprotected:{refs}")
    return 0


def cmd_repo_policy(args, cfg) -> int:
    with store.session(cfg) as conn:
        changing = (
            args.threshold is not None
            or args.role
            or args.requester_may_vote is not None
        )
        if changing:
            policy = registry.set_approval_policy(
                conn,
                args.name,
                actor=getattr(args, "agent", None) or cfg.agent,
                threshold=args.threshold,
                roles=args.role or None,
                requester_may_vote=args.requester_may_vote,
            )
            conn.commit()
        else:
            repo = registry.get_repository(conn, args.name)
            policy = registry.approval_policy(conn, repo["id"])
    print(f"repository: {args.name}")
    print(f"approval threshold: {policy['threshold']}")
    print(f"approving roles: {', '.join(policy['roles'])}")
    print(f"requester may vote: {'yes' if policy['requester_may_vote'] else 'no'}")
    with store.session(cfg) as conn:
        repo = registry.get_repository(conn, args.name)
    print(f"protected refs: {', '.join(repo['protected_refs']) or '-'}")
    print(f"protected paths: {', '.join(repo['protected_paths']) or '-'}")
    print(f"allowed ref namespaces: {', '.join(repo['allowed_ref_namespaces']) or '-'}")
    return 0


def cmd_repo_protect_path(args, cfg) -> int:
    with store.session(cfg) as conn:
        globs = registry.set_protected_path(
            conn,
            args.name,
            args.glob,
            actor=getattr(args, "agent", None) or cfg.agent,
            remove=args.remove,
        )
        conn.commit()
    print(f"protected paths for {args.name}: {', '.join(globs) or '-'}")
    return 0


def cmd_repo_allow_ref(args, cfg) -> int:
    with store.session(cfg) as conn:
        prefixes = registry.set_allowed_ref_namespace(
            conn,
            args.name,
            args.prefix,
            actor=getattr(args, "agent", None) or cfg.agent,
            remove=args.remove,
        )
        conn.commit()
    print(f"allowed ref namespaces for {args.name}: {', '.join(prefixes) or '-'}")
    return 0


def cmd_agent_add(args, cfg) -> int:
    with store.session(cfg) as conn:
        agent_id = registry.add_agent(
            conn,
            args.name,
            role=args.role,
            actor=getattr(args, "agent", None) or cfg.agent,
        )
        conn.commit()
    print(f"agent {args.name} registered as {args.role} (id {agent_id}).")
    return 0


def cmd_agent_role(args, cfg) -> int:
    with store.session(cfg) as conn:
        registry.set_agent_role(
            conn,
            args.name,
            args.role,
            actor=getattr(args, "agent", None) or cfg.agent,
        )
        conn.commit()
    print(f"agent {args.name} is now a {args.role}.")
    return 0


def cmd_agent_list(args, cfg) -> int:
    with store.session(cfg) as conn:
        for agent in registry.list_agents(conn):
            print(f"{agent['id']}\t{agent['name']}\t{agent['role']}")
    return 0


# ------------------------------------------------------------- work commands


def cmd_task_add(args, cfg) -> int:
    with store.session(cfg) as conn:
        task_id = work.create_task(conn, args.repo, args.title, args.objective)
        conn.commit()
    print(f"task {task_id} created.")
    return 0


def cmd_task_list(args, cfg) -> int:
    with store.session(cfg) as conn:
        for task in work.list_tasks(conn, repository=args.repo):
            print(f"{task['id']}\t{task['repository']}\t{task['status']}\t"
                  f"{task['title']}")
    return 0


def cmd_task_done(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        claim_id = work.complete_task(conn, args.task_id, agent, note=args.note)
        removed = args.remove_worktree and trees.cleanup_released_worktree(
            conn, claim_id, agent=agent, reason="task_done",
            event_type="worktree.task_done_cleanup",
        )
    print(f"task {args.task_id} done (claim {claim_id} released).")
    if removed:
        print("worktree removed.")
    return 0


def cmd_claim(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        takeover_operation = None
        takeover_approval = None
        if args.takeover:
            # Bind the approval to a stable incumbent. claim_task() and every
            # release take this same task lock, closing the stale-holder window.
            work.lock_task(conn, args.task_id)
            holder = work.active_claim_for_task(conn, args.task_id)
            if holder and not holder["expired"]:
                task = work.get_task(conn, args.task_id)
                takeover_operation = {
                    "type": "lease_takeover",
                    "repository": task["repository"],
                    "task_id": args.task_id,
                    "from_claim_id": holder["id"],
                    "from_agent": holder["agent"],
                    "to_agent": agent,
                }
                takeover_approval = gate.approved_instance(
                    conn, takeover_operation, consumer=agent
                )
                if takeover_approval is None:
                    refusal = gate.approval_refusal(
                        conn, takeover_operation, consumer=agent
                    )
                    print(
                        "[quorumgit] REFUSED: lease takeover requires approval.\n"
                        + (f"{refusal}\n" if refusal else "")
                        + "operation hash: "
                        f"{gate.operation_hash(takeover_operation)}\n"
                        "Request/approve it via `quorumgit approve request/vote` "
                        "with operation:\n"
                        f"{json.dumps(takeover_operation, sort_keys=True)}",
                        file=sys.stderr,
                    )
                    conn.commit()  # keep the conflict/audit trail
                    return 1
        try:
            claim_id, classification, _ = work.claim_task(
                conn,
                args.task_id,
                agent,
                branch=args.branch,
                scope_globs=args.scope,
                lease_hours=args.lease_hours,
                override_overlap=args.override_overlap,
                takeover_approved=takeover_operation is not None,
            )
        except work.ClaimRefused as exc:
            # A refused claim is non-destructive: the holder was not released
            # and the approval was not consumed. Commit only the conflict
            # event and audit trail.
            conn.commit()
            print(f"[quorumgit] REFUSED: {exc}", file=sys.stderr)
            return 1
        # The takeover succeeded: consume the approval in the same
        # transaction, so replacement claim + holder release + consumption
        # commit together or not at all.
        if takeover_operation is not None:
            try:
                assert takeover_approval is not None
                gate.consume_approval(
                    conn, takeover_approval["id"], takeover_operation, agent=agent
                )
            except gate.GateError as exc:
                conn.rollback()
                print(f"[quorumgit] REFUSED: {exc}", file=sys.stderr)
                return 1
        wt = None
        if not args.no_worktree:
            wt = trees.continue_or_create_worktree(conn, claim_id, cfg.worktrees_dir)
        conn.commit()
    print(f"claim {claim_id} acquired ({classification}).")
    if wt:
        print(f"worktree: {wt['path']}")
        if wt.get("continued_from_claim_id"):
            print(
                "continued retained worktree of claim "
                f"{wt['continued_from_claim_id']} (uncommitted work preserved)"
            )
        print(f"branch: {wt['branch']}")
    else:
        print(f"branch: {args.branch} (no worktree — work from your own clone "
              f"and push to the hub as {agent})")
    return 0


def cmd_renew(args, cfg) -> int:
    with store.session(cfg) as conn:
        work.renew_claim(conn, args.claim_id, _agent(args, cfg),
                         lease_hours=args.lease_hours)
        conn.commit()
    print(f"claim {args.claim_id} renewed.")
    return 0


def cmd_release(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        wt = trees.worktree_for_claim(conn, args.claim_id)
        if wt and wt["removed_at"] is None and args.remove_worktree:
            trees.remove_worktree(conn, args.claim_id, agent)
        work.release_claim(conn, args.claim_id, agent, reason=args.reason)
        conn.commit()
    print(f"claim {args.claim_id} released.")
    return 0


def cmd_checkpoint(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        commit = args.commit
        if not commit:
            wt = trees.active_worktree_for_claim(conn, args.claim_id)
            if wt is None:
                print("No active worktree for this claim; pass --commit <oid>.",
                      file=sys.stderr)
                return 1
            commit = trees.head_commit(wt["path"])
        cp_id = work.add_checkpoint(conn, args.claim_id, agent, commit,
                                    note=args.note)
        commit = work.checkpoint_commit(conn, cp_id)
        conn.commit()
    print(f"checkpoint {cp_id} at {commit}.")
    return 0


# ---------------------------------------------------------- handoff commands


def cmd_handoff_create(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        # An explicit --last-commit always wins; otherwise continue from the
        # managed worktree's HEAD.
        last_commit = args.last_commit
        if not last_commit:
            wt = trees.active_worktree_for_claim(conn, args.claim_id)
            if wt is None:
                print("Provide --last-commit (no active worktree for this claim).",
                      file=sys.stderr)
                return 1
            last_commit = trees.head_commit(wt["path"])
        record = {
            "completed": args.completed,
            "remaining": args.remaining,
            "last_commit": last_commit,
        }
        if args.files_changed:
            record["files_changed"] = args.files_changed
        if args.blockers:
            record["blockers"] = args.blockers
        if args.validation:
            record["validation"] = args.validation
        handoff_id = handoff.create_handoff(
            conn, args.claim_id, agent, record, to_agent=args.to
        )
        last_commit = handoff.get_handoff(conn, handoff_id)["record"]["last_commit"]
        conn.commit()
    print(f"handoff {handoff_id} created (last commit {last_commit}).")
    return 0


def cmd_handoff_list(args, cfg) -> int:
    with store.session(cfg) as conn:
        for h in handoff.list_handoffs(conn, status=args.status):
            print(f"{h['id']}\ttask {h['task_id']}\t{h['from_agent']} -> "
                  f"{h['to_agent'] or 'anyone'}\t{h['status']}")
    return 0


def cmd_handoff_show(args, cfg) -> int:
    with store.session(cfg) as conn:
        h = handoff.get_handoff(conn, args.handoff_id)
    print(json.dumps(h, indent=2, default=str))
    return 0


def cmd_handoff_accept(args, cfg) -> int:
    agent = _agent(args, cfg)
    with store.session(cfg) as conn:
        result = handoff.accept_handoff(conn, args.handoff_id, agent,
                                        lease_hours=args.lease_hours)
        conn.commit()
    print(f"claim {result['claim_id']} acquired via handoff.")
    print(f"worktree: {result['worktree'] or '(none — create manually)'}")
    print(f"branch: {result['branch']}")
    print(f"continue from commit: {result['last_commit']}")
    print(f"remaining work: {result['record']['remaining']}")
    return 0


def cmd_handoff_decline(args, cfg) -> int:
    with store.session(cfg) as conn:
        handoff.decline_handoff(conn, args.handoff_id, _agent(args, cfg))
        conn.commit()
    print(f"handoff {args.handoff_id} declined.")
    return 0


def cmd_handoff_cancel(args, cfg) -> int:
    with store.session(cfg) as conn:
        handoff.cancel_handoff(conn, args.handoff_id, _agent(args, cfg))
        conn.commit()
    print(f"handoff {args.handoff_id} cancelled.")
    return 0


# --------------------------------------------------------- approval commands


def _operation_from_args(args) -> dict:
    try:
        operation = json.loads(args.operation)
    except json.JSONDecodeError as exc:
        raise gate.GateError(f"Operation is not valid JSON: {exc}") from exc
    if not isinstance(operation, dict):
        raise gate.GateError("Operation must be a JSON object.")
    return operation


def cmd_approve_request(args, cfg) -> int:
    with store.session(cfg) as conn:
        approval = gate.request_approval(
            conn, _operation_from_args(args), requested_by=_agent(args, cfg)
        )
        conn.commit()
    print(
        f"approval {approval['id']} hash={approval['operation_hash']} "
        f"status={approval['status']} threshold={approval['threshold']}"
    )
    return 0


def cmd_approve_vote(args, cfg) -> int:
    with store.session(cfg) as conn:
        approval = gate.vote(conn, args.approval_id, _agent(args, cfg),
                             approve=not args.deny)
        conn.commit()
    print(
        f"approval {approval['id']} hash={approval['operation_hash']} "
        f"status={approval['status']}"
    )
    return 0


def cmd_approve_prepare(args, cfg) -> int:
    pusher = getattr(args, "agent", None) or cfg.agent
    if args.request and not pusher:
        raise SystemExit(
            "Agent identity required to --request: pass --agent or set "
            "QUORUMGIT_AGENT."
        )
    with store.session(cfg) as conn:
        plan = gate.prepare_push(
            conn,
            args.repo,
            args.ref,
            args.clone,
            None if args.delete else args.new,
            pusher=pusher,
        )
        if args.request and not plan["refusals"]:
            for entry in plan["operations"]:
                approval = gate.request_approval(
                    conn, entry["operation"], requested_by=pusher
                )
                entry["approval"] = {
                    key: approval[key] for key in ("id", "status", "threshold")
                }
            conn.commit()
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 1 if plan["refusals"] else 0

    print(f"repository: {plan['repository']}")
    print(f"refname: {plan['refname']}")
    print(f"oldrev: {plan['oldrev']}")
    print(f"newrev: {plan['newrev']}")
    claim = plan["claim"]
    if claim is not None:
        print(f"claim: {claim['id']} by {claim['agent']} "
              f"(scopes: {', '.join(claim['scopes'])})")
    print(f"changed paths ({len(plan['paths'])}):")
    for path in plan["paths"]:
        print(f"  {path}")
    for refusal in plan["refusals"]:
        print(f"REFUSED: {refusal}")
    if not plan["operations"]:
        print("approvals required: none")
    else:
        print(f"approvals required ({len(plan['operations'])}):")
    for entry in plan["operations"]:
        operation = entry["operation"]
        print(f"  {operation['type']} {entry['hash']}")
        for path in operation.get("paths", []):
            print(f"    {path}")
        if "approval" in entry:
            approval = entry["approval"]
            print(f"    approval {approval['id']} status={approval['status']} "
                  f"threshold={approval['threshold']}")
    if plan["operations"] and not args.request and not plan["refusals"]:
        print("re-run with --request to open these approvals.")
    return 1 if plan["refusals"] else 0


def cmd_approve_hash(args, cfg) -> int:
    print(gate.operation_hash(_operation_from_args(args)))
    return 0


# ------------------------------------------------------------- hook commands


def cmd_hook_install(args, cfg) -> int:
    with store.session(cfg) as conn:
        path = gate.install_hook(conn, args.repo)
        repo_path = registry.get_repository(conn, args.repo)["path"]
        conn.commit()
    print(f"pre-receive hook installed: {path}")
    print("reference-transaction hook installed: "
          f"{gate._effective_hook(repo_path, 'reference-transaction')}")
    return 0


def cmd_hook_pre_receive(args, cfg) -> int:
    with store.session(cfg) as conn:
        return gate.run_pre_receive(conn, args.repo, sys.stdin)


def cmd_hook_reference_transaction(args, cfg) -> int:
    if not store.database_path(cfg).exists():
        # Without a store, only unidentified local ref maintenance may pass.
        # Every push is identified (pre-receive requires it), so a store that
        # vanished after pre-receive must not wave its update through.
        if args.state == "prepared" and cfg.agent:
            print(
                "[quorumgit] REJECTED: the store is missing; cannot re-validate "
                f"this update by {cfg.agent}.",
                file=sys.stderr,
            )
            return 1
        return 0
    try:
        conn = store.connect(cfg)
    except store.StoreError as exc:
        label = "REJECTED" if args.state == "prepared" else f"WARNING ({args.state})"
        print(f"[quorumgit] {label}: {exc}", file=sys.stderr)
        return 1
    try:
        with conn:
            return gate.run_reference_transaction(
                conn, args.repo, args.state, sys.stdin
            )
    finally:
        conn.close()


# -------------------------------------------------------------------- audit


def cmd_audit(args, cfg) -> int:
    with store.session(cfg) as conn:
        for event in audit.events(conn, entity=args.entity,
                                  entity_id=args.entity_id, limit=args.limit):
            print(f"{event['id']}\t{event['created_at']}\t{event['event_type']}"
                  f"\t{event['entity']}:{event['entity_id']}"
                  f"\t{event['agent'] or '-'}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quorumgit")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, configure=None, parent=None):
        sp = (parent or sub).add_parser(name)
        if configure:
            configure(sp)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init)
    add("status", cmd_status)
    add("destroy", cmd_destroy,
        lambda sp: sp.add_argument("--yes", action="store_true"))
    add("doctor", cmd_doctor,
        lambda sp: sp.add_argument("--repair", action="store_true"))

    repo = sub.add_parser("repo").add_subparsers(dest="sub", required=True)
    add("add", cmd_repo_add, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("path"),
        sp.add_argument("--protected-ref", action="append", default=[]),
        sp.add_argument("--protected-path", action="append", default=[],
                        help="path glob whose changes always need an approval"),
    ), parent=repo)
    add("list", cmd_repo_list, parent=repo)
    add("policy", cmd_repo_policy, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("--agent", help="operator making the change"),
        sp.add_argument("--threshold", type=int,
                        help="eligible yes votes required"),
        sp.add_argument("--role", action="append", default=[],
                        choices=registry.ROLES,
                        help="role whose votes count (repeatable; replaces "
                             "the current set)"),
        sp.add_argument("--requester-may-vote", dest="requester_may_vote",
                        action=argparse.BooleanOptionalAction, default=None),
    ), parent=repo)
    add("protect-path", cmd_repo_protect_path, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("glob"),
        sp.add_argument("--remove", action="store_true"),
        sp.add_argument("--agent", help="operator making the change"),
    ), parent=repo)
    add("allow-ref", cmd_repo_allow_ref, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("prefix", help="ref namespace such as refs/tags/"),
        sp.add_argument("--remove", action="store_true"),
        sp.add_argument("--agent", help="operator making the change"),
    ), parent=repo)

    agent = sub.add_parser("agent").add_subparsers(dest="sub", required=True)
    add("add", cmd_agent_add, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("--role", choices=registry.ROLES,
                        default=registry.DEFAULT_ROLE),
        sp.add_argument("--agent", help="operator registering a non-worker"),
    ), parent=agent)
    add("role", cmd_agent_role, lambda sp: (
        sp.add_argument("name"),
        sp.add_argument("role", choices=registry.ROLES),
        sp.add_argument("--agent", help="operator making the change"),
    ), parent=agent)
    add("list", cmd_agent_list, parent=agent)

    task = sub.add_parser("task").add_subparsers(dest="sub", required=True)
    add("add", cmd_task_add, lambda sp: (
        sp.add_argument("--repo", required=True),
        sp.add_argument("--title", required=True),
        sp.add_argument("--objective", default=""),
    ), parent=task)
    add("list", cmd_task_list,
        lambda sp: sp.add_argument("--repo"), parent=task)
    add("done", cmd_task_done, lambda sp: (
        sp.add_argument("task_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--note", default=""),
        sp.add_argument("--remove-worktree", action="store_true",
                        help="remove the claim's worktree; refused if it has "
                             "uncommitted changes"),
    ), parent=task)

    add("claim", cmd_claim, lambda sp: (
        sp.add_argument("task_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--branch", required=True),
        sp.add_argument("--scope", action="append", default=[]),
        sp.add_argument("--lease-hours", type=float,
                        default=work.DEFAULT_LEASE_HOURS),
        sp.add_argument("--override-overlap", action="store_true"),
        sp.add_argument("--no-worktree", action="store_true",
                        help="reserve the claim without creating a worktree "
                             "(hub deployments: work from your own clone)"),
        sp.add_argument("--takeover", action="store_true"),
    ))
    add("renew", cmd_renew, lambda sp: (
        sp.add_argument("claim_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--lease-hours", type=float,
                        default=work.DEFAULT_LEASE_HOURS),
    ))
    add("release", cmd_release, lambda sp: (
        sp.add_argument("claim_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--reason", default="released"),
        sp.add_argument("--remove-worktree", action="store_true"),
    ))
    add("checkpoint", cmd_checkpoint, lambda sp: (
        sp.add_argument("claim_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--note", default=""),
        sp.add_argument("--commit", help="commit OID (required when the claim "
                                         "has no worktree)"),
    ))

    ho = sub.add_parser("handoff").add_subparsers(dest="sub", required=True)
    add("create", cmd_handoff_create, lambda sp: (
        sp.add_argument("claim_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--completed", required=True),
        sp.add_argument("--remaining", required=True),
        sp.add_argument("--to"),
        sp.add_argument("--files-changed", action="append", default=[]),
        sp.add_argument("--blockers", action="append", default=[]),
        sp.add_argument("--validation", default=""),
        sp.add_argument("--last-commit"),
    ), parent=ho)
    add("list", cmd_handoff_list,
        lambda sp: sp.add_argument("--status"), parent=ho)
    add("show", cmd_handoff_show,
        lambda sp: sp.add_argument("handoff_id", type=int), parent=ho)
    add("accept", cmd_handoff_accept, lambda sp: (
        sp.add_argument("handoff_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--lease-hours", type=float,
                        default=work.DEFAULT_LEASE_HOURS),
    ), parent=ho)
    add("decline", cmd_handoff_decline, lambda sp: (
        sp.add_argument("handoff_id", type=int),
        sp.add_argument("--agent"),
    ), parent=ho)
    add("cancel", cmd_handoff_cancel, lambda sp: (
        sp.add_argument("handoff_id", type=int),
        sp.add_argument("--agent"),
    ), parent=ho)

    ap = sub.add_parser("approve").add_subparsers(dest="sub", required=True)
    add("request", cmd_approve_request, lambda sp: (
        sp.add_argument("operation", help="operation JSON object"),
        sp.add_argument("--agent"),
    ), parent=ap)
    add("vote", cmd_approve_vote, lambda sp: (
        sp.add_argument("approval_id", type=int),
        sp.add_argument("--agent"),
        sp.add_argument("--deny", action="store_true"),
    ), parent=ap)
    add("prepare", cmd_approve_prepare, lambda sp: (
        sp.add_argument("--repo", required=True),
        sp.add_argument("--ref", required=True,
                        help="ref to push (a bare name means refs/heads/<name>)"),
        sp.add_argument("--new", default="HEAD",
                        help="revision in your clone to push (default HEAD)"),
        sp.add_argument("--delete", action="store_true",
                        help="plan deleting the ref instead"),
        sp.add_argument("-C", dest="clone", default=".",
                        help="your clone (default: current directory)"),
        sp.add_argument("--request", action="store_true",
                        help="open an approval for every required operation"),
        sp.add_argument("--json", action="store_true"),
        sp.add_argument("--agent"),
    ), parent=ap)
    add("hash", cmd_approve_hash,
        lambda sp: sp.add_argument("operation"), parent=ap)

    hook = sub.add_parser("hook").add_subparsers(dest="sub", required=True)
    add("install", cmd_hook_install,
        lambda sp: sp.add_argument("--repo", required=True), parent=hook)
    add("pre-receive", cmd_hook_pre_receive,
        lambda sp: sp.add_argument("--repo", required=True), parent=hook)
    add("reference-transaction", cmd_hook_reference_transaction, lambda sp: (
        sp.add_argument("--repo", required=True),
        sp.add_argument("state"),
    ), parent=hook)

    add("audit", cmd_audit, lambda sp: (
        sp.add_argument("--entity"),
        sp.add_argument("--entity-id", type=int),
        sp.add_argument("--limit", type=int, default=100),
    ))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config.load()
    try:
        return args.fn(args, cfg)
    except (
        store.StoreError,
        registry.RegistryError,
        work.WorkError,
        trees.WorktreeError,
        gate.GateError,
        handoff.HandoffError,
        config.ConfigError,
        git_objects.GitObjectError,
    ) as exc:
        print(f"[quorumgit] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
