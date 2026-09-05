# QuorumGit

**Git governance for multiple coding agents sharing one repository.**

QuorumGit prevents coding agents (or humans) working the same codebase from stepping on each other: claiming the same task twice, editing the same checkout, touching overlapping paths, abandoning work without a trail, or force-pushing over someone else's branch. It is a CLI and an embedded database — no daemon, no server, no background process — and every rule it enforces leaves an append-only audit record.

```
agent-one ──┐
agent-two ──┼──► quorumgit CLI ──► embedded PostgreSQL (claims, leases, approvals, audit)
reviewer  ──┘                          │
                                       └──► git pre-receive hook (push enforcement)
```

## The problem it solves

Run two or more autonomous coding agents against one repository and, without coordination, you get:

- **Duplicate work** — two agents independently pick up the same task.
- **Trampled checkouts** — both edit the same working directory.
- **Overlapping edits** — different tasks, same files, silent conflicts.
- **Abandoned work** — an agent's session ends and nobody knows what was done, what remains, or which commit to continue from.
- **Ungoverned destruction** — force pushes, ref deletions, and branch takeovers with no approval and no record.

QuorumGit makes each of these either impossible or explicitly governed, using mechanisms Git and PostgreSQL already provide: worktrees, pre-receive hooks, row locks, and append-only tables.

## The mental model

Seven concepts, in the order you meet them:

| Concept | What it is |
|---|---|
| **Repository** | A registered Git repo that QuorumGit governs. |
| **Agent** | A registered identity. Set via `QUORUMGIT_AGENT` or `--agent`. |
| **Task** | A unit of work against one repository. |
| **Claim** | An agent's exclusive lease on a task: names a branch, declares write **scopes** (path globs), and expires at a timestamp. Expired leases make the task reclaimable — evaluated at read time, no timers. |
| **Worktree** | An isolated `git worktree` created per claim. Agents never share a mutable checkout; Git itself refuses to check one branch out twice. |
| **Handoff** | A structured continuation record (done / remaining / exact commit / blockers) that transfers work to a successor instead of abandoning it. |
| **Approval** | An operator sign-off, hash-bound to one exact operation (a specific push, takeover, or deletion), consumed on use. |

Everything an agent does — claim, renew, checkpoint, hand off, release — writes an audit event in the same database transaction. The audit table is append-only, enforced by a trigger.

## Installation

Requirements: **Python 3.13 or 3.14**, **git**. PostgreSQL is *not* a prerequisite — QuorumGit embeds its own instance via [pg0](https://github.com/vectorize-io/pg0).

```bash
# recommended: uv (editable, so a git pull updates the live tool)
uv tool install --editable /path/to/QuorumGit

# or plain pip
pip install /path/to/QuorumGit
```

Then initialize the store once:

```bash
$ quorumgit init
store: postgresql://postgres:postgres@127.0.0.1:5434/quorumgit
migrations applied: ['001_core.sql', '002_handoff_cancel.sql']
contract: ok
```

`init` starts the embedded PostgreSQL instance, installs the `pg_jsonschema` extension if missing (fetched over HTTPS from the pinned upstream release and verified against a hard-coded SHA-256 before install), applies migrations, and verifies the runtime contract. It is idempotent — re-run it any time; it also repairs the extension after a PostgreSQL upgrade.

`quorumgit status` shows the store URI, contract state, and row counts. If the store is down or incomplete, every command exits non-zero: there is **one storage backend and no fallback**, by design.

## Quick start (five minutes)

Register a repository and two agents, then run one full work cycle:

```bash
quorumgit repo add myproject /path/to/working-repo
quorumgit agent add agent-one
quorumgit agent add agent-two
export QUORUMGIT_AGENT=agent-one

quorumgit task add --repo myproject --title "implement feature X"
# task 1 created.

quorumgit claim 1 --branch feat/x --scope 'src/**'
# claim 1 acquired (CLEAR).
# worktree: ~/.quorumgit/worktrees/myproject/task-1-agent-one
# branch: feat/x
```

The agent now works **inside that worktree** — edits, commits, tests — without touching anyone else's checkout. Along the way:

```bash
quorumgit checkpoint 1 --note "parser done, tests green"
# checkpoint 1 at <commit-oid>.
```

When the agent is done, either release:

```bash
quorumgit release 1 --remove-worktree
```

…or hand the work to someone else with everything they need to continue:

```bash
quorumgit handoff create 1 \
    --completed "parser implemented, unit tests pass" \
    --remaining "wire into CLI, integration tests" \
    --to agent-two

QUORUMGIT_AGENT=agent-two quorumgit handoff accept 1
# claim 2 acquired. Same worktree, exact commit to continue from.
```

What just *didn't* happen, silently: a second agent claiming task 1 (**BLOCKED**), claiming another task on branch `feat/x` (**CONFLICTING**), or claiming a task whose scopes overlap `src/**` (**OVERLAPPING**, refused unless explicitly overridden and audited). While the handoff was open, the task was **reserved** — not claimable by third parties, its branch frozen — until agent-two accepted.

## Conflict classification

Every claim attempt is classified against all live claims before it is granted, and every classification is recorded whether or not the claim proceeds:

| Classification | Meaning | Result |
|---|---|---|
| `CLEAR` | no other live claims | granted |
| `RELATED` | live claims exist, scopes disjoint | granted |
| `OVERLAPPING` | declared scopes intersect another claim's | refused unless `--override-overlap` (audited governance override) |
| `CONFLICTING` | branch already claimed by another task | refused |
| `BLOCKED` | task already held by an unexpired claim | refused; takeover requires approval |

Scope overlap uses a conservative literal-prefix test (the prefix of one glob up to its first wildcard against the other's) — it errs toward flagging.

## Two deployment models

Where the registered repository sits determines how enforcement works. Pick one per repository; don't mix them.

**Local model** — the registered repo is a working checkout on the same machine. Claims create isolated worktrees; agents commit directly in them. Coordination is entirely claim-based; no pushes, no hook needed. This is the default and the simplest setup — the quick start above is the local model.

**Hub model** — the registered repo is a **bare** hub that agents push to from their own clones. Enforcement moves to a pre-receive hook:

```bash
git clone --bare /path/to/source myproject.git
quorumgit repo add myproject /path/to/myproject.git --protected-ref refs/heads/main
quorumgit hook install --repo myproject

quorumgit task add --repo myproject --title "implement feature X"
QUORUMGIT_AGENT=agent-one quorumgit claim 1 --branch feat/x --scope 'src/**' --no-worktree
```

`--no-worktree` matters: a managed worktree on the hub would check the branch out and make Git refuse the very push the hook just authorized. The agent works in its own clone and pushes as its registered identity:

```bash
QUORUMGIT_AGENT=agent-one git push origin feat/x    # accepted — owner
QUORUMGIT_AGENT=agent-two git push origin feat/x    # rejected — branch is claimed
git push origin feat/x                              # rejected — unidentified
```

Checkpoints in the hub model take an explicit `--commit <oid>`. Continuation points are **verified, not trusted**: the commit must exist in the registered repository, and when the claimed branch exists, be reachable from it. A typo'd or fabricated OID is rejected.

## Protected operations and approvals

Updates to protected refs, force pushes, ref deletions, and lease takeovers all require an approval. An approval is bound by SHA-256 hash to the **exact operation payload** (canonical JSON) — approving one push authorizes that push at those exact revisions, and nothing else.

The flow, driven by the rejection messages themselves:

```bash
# 1. An agent pushes to a protected ref; the hook rejects and prints the operation hash:
#    protected_ref_update on refs/heads/main requires an approval
#    bound to this exact update (hash sha256:ab12…).

# 2. The operator approves that exact operation:
quorumgit approve request '{"type":"protected_ref_update","repository":"myproject","refname":"refs/heads/main","oldrev":"<old>","newrev":"<new>"}'
quorumgit approve vote sha256:ab12… --agent operator

# 3. The same push now lands. Pushing it again — or replaying the approval — is rejected:
#    the approval was consumed atomically when the operation was accepted.
```

Rules that hold no matter what:

- **An approval never bypasses branch reservations.** A claimed branch still accepts pushes only from its claiming agent; a branch frozen by an open handoff accepts none at all. The hook checks reservations *before* approvals.
- **Denial has precedence and is terminal.** One `--deny` vote denies the approval; later yes votes are refused.
- **Consumption is single-use under concurrency.** Two simultaneous pushes racing for one approval produce exactly one accepted push — the loser is rejected, not silently allowed.
- The default threshold is 1 (a human operator); the vote schema supports higher thresholds.

Takeovers follow the same pattern: claiming a task someone else holds (`claim <task> --takeover`) prints the takeover operation to approve. The takeover is atomic — the incumbent is released, the replacement claim created, and the approval consumed in one transaction, or none of it happens. A refused takeover leaves the incumbent untouched and the approval unconsumed.

## Command reference

| Command | Purpose |
|---|---|
| `quorumgit init` | Start/provision the store (idempotent) |
| `quorumgit status` | Store health, contract check, row counts |
| `quorumgit destroy --yes` | Stop the store and delete its data |
| `quorumgit repo add <name> <path> [--protected-ref <ref>]…` | Register a repository |
| `quorumgit agent add <name>` | Register an agent identity |
| `quorumgit task add --repo <name> --title <t> [--objective <o>]` | Create a task |
| `quorumgit claim <task> --branch <b> --scope <glob>… [--no-worktree] [--takeover] [--override-overlap] [--lease-hours <h>]` | Claim a task |
| `quorumgit renew <claim>` | Extend a lease |
| `quorumgit checkpoint <claim> [--commit <oid>] [--note <n>]` | Record verified progress |
| `quorumgit release <claim> [--remove-worktree] [--reason <r>]` | Release a claim |
| `quorumgit handoff create <claim> --completed <c> --remaining <r> [--to <agent>] [--last-commit <oid>]` | Hand work off |
| `quorumgit handoff accept <id>` | Continue handed-off work (addressee, or anyone if unaddressed) |
| `quorumgit handoff decline <id>` | Decline — addressee only |
| `quorumgit handoff cancel <id>` | Cancel — creator only |
| `quorumgit handoff list / show <id>` | Inspect handoffs |
| `quorumgit approve request <json> [--threshold <n>]` | Open an approval for an exact operation |
| `quorumgit approve vote <hash> [--deny]` | Vote |
| `quorumgit approve hash <json>` | Compute an operation's hash |
| `quorumgit hook install --repo <name>` | Install the pre-receive hook (hub model) |
| `quorumgit audit [--entity <e>] [--entity-id <id>] [--limit <n>]` | Read the audit trail |

`repo list`, `agent list`, and `task list [--repo <name>]` enumerate what's registered. Exit codes: `0` success, `1` refused/violation/error, `2` usage error.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `QUORUMGIT_DATA_DIR` | State root: pg0 data and managed worktrees | `~/.quorumgit` |
| `QUORUMGIT_PG_INSTANCE` | Name of the QuorumGit-owned pg0 instance | `quorumgit` |
| `QUORUMGIT_PG_PORT` | Local PostgreSQL port | selected automatically |
| `QUORUMGIT_AGENT` | Agent identity for CLI commands and governed pushes | unset |

There is deliberately no external-database option and no connection-string configuration: QuorumGit owns one embedded instance and fails loudly when it is unavailable.

## Design properties

- **CLI-only, no daemon.** Every operation is a short-lived transaction. Lease expiry is computed from timestamps at read time — there is no scheduler, no cron, nothing to keep alive except the embedded PostgreSQL that pg0 manages.
- **One store, fail-loud.** No SQLite fallback, no file mode, no degraded operation. If the store or a required extension is missing, commands exit non-zero and say why.
- **Concurrency-safe where it counts.** Approval voting and consumption, takeover ownership transitions, and handoff resolution all serialize on row locks (`SELECT … FOR UPDATE`) with guarded conditional updates, in a consistent lock order. Races produce exactly one winner and an explicit error for the loser — verified by concurrent two-connection tests, not by inspection.
- **Verified continuation.** Checkpoint and handoff commits must exist in the registered repository (and be reachable from the claimed branch when it exists). The continuation contract survives restarts: stop the store mid-workflow and the claims, handoffs, and audit history are intact when it returns.
- **Structured artifacts are schema-checked in the database.** Handoff records and approval operations are JSONB validated by `pg_jsonschema` CHECK constraints.

## Threat model — read this honestly

QuorumGit v1 coordinates **cooperating agents**; the adversary is *accident, not malice*.

- Agent identity is asserted (`QUORUMGIT_AGENT`), not cryptographically authenticated. Any local process can claim to be any agent.
- The trust root is write access to the database and the filesystem. An actor with either can bypass governance.
- The pre-receive hook governs `git push` only. Direct ref manipulation inside a repository bypasses it.
- Approval hashes provide exact-payload binding and tamper-evidence, not approver authentication.

These are the correct trade-offs for preventing well-intentioned agents from colliding on one machine. They are not Byzantine fault tolerance, and this document will not pretend otherwise.

## Non-goals (v1)

No daemon. No network transport or multi-node consensus. No secondary storage backend of any kind. No automatic task assignment. No MCP server (a thin wrapper over this CLI is a natural later addition).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q
ruff check src tests
pyright
```

Python 3.13 or 3.14. Tests run against a live throwaway pg0 instance and real Git repositories — nothing is mocked. The suite covers the full acceptance workflow (register → claim → isolate → block overlap → parallel work → checkpoint → handoff → accept → approval-gated takeover → audit → restart survival), real-push hook enforcement, concurrent voting/consumption/resolution races, and a branding gate that keeps application code free of any identity other than QuorumGit.

## License

MIT
