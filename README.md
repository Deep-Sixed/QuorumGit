# QuorumGit

**Git governance for multiple coding agents sharing one repository.**

QuorumGit prevents coding agents (or humans) working the same codebase from stepping on each other: claiming the same task twice, editing the same checkout, touching overlapping paths, abandoning work without a trail, or force-pushing over someone else's branch. It is a CLI and an embedded database — no daemon, no server, no background process — and every rule it enforces leaves an append-only audit record.

```
agent-one ──┐
agent-two ──┼──► quorumgit CLI ──► local SQLite file (claims, leases, approvals, audit)
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

QuorumGit makes each of these either impossible or explicitly governed, using mechanisms Git and SQLite already provide: worktrees, pre-receive hooks, single-writer transactions, and append-only tables.

## The mental model

Seven concepts, in the order you meet them:

| Concept | What it is |
|---|---|
| **Repository** | A registered Git repo that QuorumGit governs. |
| **Agent** | A registered identity. Set via `QUORUMGIT_AGENT` or `--agent`. |
| **Task** | A unit of work against one repository. |
| **Claim** | An agent's exclusive lease on a task: names a branch, declares at least one write **scope** (path glob), and expires at a timestamp (default lease: 8 hours, set with `--lease-hours`). Expired leases make the task reclaimable and cannot be renewed — evaluated at read time, no timers. |
| **Worktree** | An isolated `git worktree` created per claim. Agents never share a mutable checkout; Git itself refuses to check one branch out twice. |
| **Handoff** | A structured continuation record (done / remaining / exact commit / blockers) that transfers work to a successor instead of abandoning it. |
| **Approval** | A sign-off by registered agents, hash-bound to one exact operation (a specific push, takeover, or deletion), consumed on use. |

Everything an agent does — claim, renew, checkpoint, hand off, release — writes an audit event in the same database transaction. The audit table is append-only, enforced by a trigger.

## Installation

Requirements: **Python 3.11–3.14**, **git**. There is no database to install, no server to run, and no extension to provision: the store is a single SQLite 3 database file under `QUORUMGIT_DATA_DIR`, accessed through Python's standard-library [`sqlite3`](https://docs.python.org/3/library/sqlite3.html) module. QuorumGit has no third-party runtime dependencies. The Python interpreter must be linked against SQLite 3.38 or newer (every supported CPython release from python.org, Homebrew, and current Linux distributions is); `quorumgit` refuses to open the store otherwise.

> **Platform note.** Linux, macOS, and Windows are supported on every Python from 3.11 to 3.14.

```bash
# recommended: uv (editable, so a git pull updates the live tool)
uv tool install --editable /path/to/QuorumGit

# or plain pip
pip install /path/to/QuorumGit
```

Then initialize the store once:

```bash
$ quorumgit init
store: /home/you/.quorumgit/quorumgit.db
migrations applied: ['001_core.sql', '002_approval_identities.sql']
contract: ok
```

`init` creates the state directory, applies migrations, and verifies the runtime contract (required tables, foreign keys on, WAL journal mode). It is idempotent — re-run it any time.

`quorumgit status` shows the store path, contract state, and row counts. If the store is missing or incomplete, every command exits non-zero: there is **one storage backend and no fallback**, by design.

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
# worktree: ~/.quorumgit/worktrees/myproject/task-1-agent-one-claim-1
# branch: feat/x
```

The agent now works **inside that worktree** — edits, commits, tests — without touching anyone else's checkout. Along the way:

```bash
quorumgit checkpoint 1 --note "parser done, tests green"
# checkpoint 1 at <commit-oid>.
```

When the agent stops working, either release:

```bash
quorumgit release 1 --remove-worktree
```

Releasing returns the task to `open`, so any agent can claim it again. There is currently no command that marks a task finished — release means "I am no longer working on this", not "this is done".

…or hand the work to someone else with everything they need to continue:

```bash
quorumgit handoff create 1 \
    --completed "parser implemented, unit tests pass" \
    --remaining "wire into CLI, integration tests" \
    --to agent-two

QUORUMGIT_AGENT=agent-two quorumgit handoff accept 1
# claim 2 acquired via handoff.
# worktree: ~/.quorumgit/worktrees/myproject/task-1-agent-one-claim-1
# branch: feat/x
# continue from commit: <commit-oid>
# remaining work: wire into CLI, integration tests
```

Creating a handoff releases the original claim but keeps its worktree; accepting transfers that same worktree to the new claim. When the claim has an active worktree, the handoff records the worktree's `HEAD` as the last commit; `--last-commit` is used when there is none (hub model, or a worktree removed by `doctor --repair`).

What just *didn't* happen, silently: a second agent claiming task 1 (**BLOCKED**), claiming another task on branch `feat/x` (**CONFLICTING**), or claiming a task whose scopes overlap `src/**` (**OVERLAPPING**, refused unless explicitly overridden and audited). While the handoff was open, the task, branch, and declared scopes were **reserved** for continuation until agent-two accepted.

## Conflict classification

Every claim attempt is classified against all live claims before it is granted, and every classification is recorded whether or not the claim proceeds:

| Classification | Meaning | Result |
|---|---|---|
| `CLEAR` | no live claims on other tasks in the same repository | granted |
| `RELATED` | live claims exist on other tasks, scopes disjoint | granted |
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

The hook learns who is pushing from `QUORUMGIT_AGENT` in its own environment, which Git passes through only when the remote is a **local path** (as `origin` is above). Over SSH or HTTP the variable does not reach the hook, so every push is rejected as unidentified. The hook also opens the store under its own `QUORUMGIT_DATA_DIR`, so pushers must use the same data directory as the rest of the deployment.

Accepting a handoff in the hub model prints `worktree: (none — create manually)`: there is no managed worktree to transfer, so the successor fetches the branch into its own clone and continues from the reported commit.

Checkpoints in the hub model take an explicit `--commit <oid>`. Continuation points are **verified, not trusted**: the commit must exist in the registered repository, and when the claimed branch exists, be reachable from it. A typo'd or fabricated OID is rejected.

## Protected operations and approvals

Updates to protected refs, force pushes, ref deletions, and lease takeovers all require an approval. An approval is bound by SHA-256 hash to the **exact operation payload** (canonical JSON) — approving one push authorizes that push at those exact revisions, and nothing else.

The flow, driven by the rejection messages themselves:

```bash
# 1. An agent pushes to a protected ref; the hook rejects and prints the operation hash:
#    protected_ref_update on refs/heads/main requires an approval
#    bound to this exact update (hash sha256:ab12…).

# 2. Any registered agent requests that exact operation, and an approver votes
#    on the returned approval instance ID. The approver must be registered too:
quorumgit agent add operator
quorumgit approve request '{"type":"protected_ref_update","repository":"myproject","refname":"refs/heads/main","oldrev":"<old>","newrev":"<new>"}'
# approval 17 hash=sha256:ab12… status=pending
quorumgit approve vote 17 --agent operator

# 3. The same push now lands. Pushing it again — or replaying the approval — is rejected:
#    the approval was consumed atomically when the operation was accepted.
```

Rules that hold no matter what:

- **An approval never bypasses branch reservations.** A claimed branch still accepts pushes only from its claiming agent; a branch frozen by an open handoff accepts none at all. The hook checks reservations *before* approvals.
- **Denial has precedence and is terminal.** One `--deny` vote denies the approval; later yes votes are refused.
- **Consumption is single-use under concurrency.** Two simultaneous pushes racing for one approval produce exactly one accepted push — the loser is rejected, not silently allowed.
- **Votes bind to one approval instance.** A delayed vote for an older denied or consumed instance cannot decide a newer request with the same operation hash. Requesters, voters, consumers, and pushers must name registered agents.
- **A consumed approval is spent, not blacklisted.** Consumption moves the approval to a terminal `consumed` state (with `consumed_at`) rather than reusing `denied`, and only one *live* (`pending` or `approved`) approval may exist per operation hash. The same operation can therefore be requested and approved again later as a new approval instance — which matters for takeovers, whose payload is stable and legitimately repeatable. What is never possible is one approval authorizing twice.
- The default threshold is 1; `--threshold` sets a higher one. "Operator" is a convention, not a role: **any registered agent can vote, including the agent that requested the approval or is making the push.** Keeping approvals in human hands depends on how agent identities are used, not on anything QuorumGit enforces (see the threat model).

Takeovers follow the same pattern: claiming a task someone else holds (`claim <task> --takeover`) prints the takeover operation to approve. Its payload includes the incumbent claim ID, so an unused approval cannot displace a later claim by the same agent. The takeover is atomic — the incumbent is released, the replacement claim created, and the approval consumed in one transaction, or none of it happens. A refused takeover leaves the incumbent untouched and the approval unconsumed.

## Command reference

| Command | Purpose |
|---|---|
| `quorumgit init` | Start/provision the store (idempotent) |
| `quorumgit status` | Store health, contract check, row counts |
| `quorumgit doctor [--repair]` | Detect and conservatively reconcile recorded managed-worktree drift |
| `quorumgit destroy --yes` | Delete the database file (managed worktrees under `QUORUMGIT_DATA_DIR/worktrees` are left in place) |
| `quorumgit repo add <name> <path> [--protected-ref <ref>]…` | Register a repository |
| `quorumgit agent add <name>` | Register an agent identity |
| `quorumgit task add --repo <name> --title <t> [--objective <o>]` | Create a task |
| `quorumgit claim <task> --branch <b> --scope <glob>… [--no-worktree] [--takeover] [--override-overlap] [--lease-hours <h>]` | Claim a task |
| `quorumgit renew <claim> [--lease-hours <h>]` | Extend a live, unexpired lease; expired claims must be acquired again |
| `quorumgit checkpoint <claim> [--commit <oid>] [--note <n>]` | Record verified progress |
| `quorumgit release <claim> [--remove-worktree] [--reason <r>]` | Release a claim; the task returns to `open` |
| `quorumgit handoff create <claim> --completed <c> --remaining <r> [--to <agent>] [--files-changed <f>]… [--blockers <b>]… [--validation <v>] [--last-commit <oid>]` | Hand work off (`--last-commit` only when the claim has no active worktree) |
| `quorumgit handoff accept <id> [--lease-hours <h>]` | Continue handed-off work (addressee, or anyone if unaddressed) |
| `quorumgit handoff decline <id>` | Decline — addressee only; removes the retained worktree, and is refused if it has uncommitted changes |
| `quorumgit handoff cancel <id>` | Cancel — creator only; removes the retained worktree, and is refused if it has uncommitted changes |
| `quorumgit handoff list [--status <s>] / show <id>` | Inspect handoffs |
| `quorumgit approve request <json> [--threshold <n>]` | Open an approval for an exact operation |
| `quorumgit approve vote <approval-id> [--deny]` | Vote on one approval instance |
| `quorumgit approve hash <json>` | Compute an operation's hash |
| `quorumgit hook install --repo <name>` | Install the pre-receive hook (hub model) |
| `quorumgit audit [--entity <e>] [--entity-id <id>] [--limit <n>]` | Read the audit trail |

`repo list`, `agent list`, and `task list [--repo <name>]` enumerate what's registered. Commands that act as an agent (`claim`, `renew`, `release`, `checkpoint`, `handoff create/accept/decline/cancel`, `approve request/vote`) also take `--agent <name>`, which overrides `QUORUMGIT_AGENT`. At least one `--scope` is required to claim. Exit codes: `0` success, `1` refused/violation/error, `2` usage error.

`quorumgit doctor` only checks worktree paths already recorded by QuorumGit. It reports:

- `missing` — recorded as active, but the directory is gone. `--repair` prunes Git's stale worktree metadata and marks the record removed.
- `orphaned` — the claim is released with no open handoff, but the checkout remains. `--repair` removes it.
- `unexpectedly_present` — recorded as removed, but the directory exists again. `--repair` removes it.
- `repository_mismatch`, `branch_mismatch`, `detached_head`, `unverifiable_checkout` — the checkout at the recorded path no longer matches the recorded repository and branch. These are never repaired automatically; inspect them by hand.

Repairs use ordinary non-forced `git worktree remove`, so dirty worktrees are reported instead of destroyed. `doctor` exits non-zero while any finding remains unresolved.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `QUORUMGIT_DATA_DIR` | State root: `quorumgit.db` and managed worktrees | `~/.quorumgit` |
| `QUORUMGIT_AGENT` | Agent identity for CLI commands and governed pushes | unset |

There is deliberately no external-database option and no connection-string configuration: QuorumGit owns one local database file and fails loudly when it is missing or fails its contract check.

## Design properties

- **CLI-only, no daemon.** Every operation is a short-lived transaction. Lease expiry is computed from timestamps at read time. There is no scheduler, no cron, and nothing to keep alive: the store is a file.
- **One store, fail-loud.** One SQLite file, no second backend, no degraded operation. If the store is missing or fails its contract check, commands exit non-zero and say why.
- **Concurrency-safe where it counts.** Every governance write takes the database's single-writer reservation (`BEGIN IMMEDIATE`) before it reads, and pairs it with guarded conditional updates. Approval voting and consumption, takeover ownership transitions, and handoff resolution all serialize; races produce exactly one winner and an explicit error for the loser — verified by concurrent two-connection tests, not by inspection. Because the reservation covers the whole database rather than one row, it also closes the cross-task branch collision that per-task row locks did not.
- **Verified continuation.** Checkpoint and handoff commits must exist in the registered repository (and be reachable from the claimed branch when it exists). The continuation contract survives restarts: every state change is committed to the database file, so claims, handoffs, and audit history are intact for the next process that opens it.
- **Structured artifacts are schema-checked in the database.** Handoff and approval fields the governance rules depend on are relational columns with `CHECK` constraints; JSON is retained only for open-ended arrays and detail blobs.

## Threat model — read this honestly

QuorumGit v1 coordinates **cooperating agents**; the adversary is *accident, not malice*.

- Agent identity is asserted (`QUORUMGIT_AGENT`), not cryptographically authenticated. Any local process can claim to be any agent.
- The trust root is write access to the database and the filesystem. An actor with either can bypass governance.
- The pre-receive hook governs `git push` only. Direct ref manipulation inside a repository bypasses it.
- Approval hashes provide exact-payload binding and tamper-evidence, not approver authentication. Any registered agent may vote, including the requester or pusher, so an agent can approve its own protected operation.

These are the correct trade-offs for preventing well-intentioned agents from colliding on one machine. They are not Byzantine fault tolerance, and this document will not pretend otherwise.

## Non-goals (v1)

No daemon. No network transport or multi-node consensus. No secondary storage backend of any kind. No automatic task assignment. No MCP server (a thin wrapper over this CLI is a natural later addition).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m pytest tests/ -q
ruff check src tests
pyright
```

Supported interpreters: Python 3.11 through 3.14; `ruff` and `pyright` are pinned to the 3.11 floor so post-3.11 syntax and APIs fail the checks. CI runs `pyright` and the test suite on Linux, macOS, and Windows with Python 3.11 and 3.14; it does not run `ruff`, so run it locally. Tests run against a real SQLite database and real Git repositories — nothing is mocked. The suite covers the full acceptance workflow (register → claim → isolate → block overlap → parallel work → checkpoint → handoff → accept → approval-gated takeover → audit → restart survival), real-push hook enforcement, concurrent voting/consumption/resolution races, and a branding gate that keeps application code free of any identity other than QuorumGit.

Design notes: [`docs/parallel-code-review.md`](docs/parallel-code-review.md) records which ideas from the Parallel Code project were adopted (the checkout-identity checks in `doctor`) and which were rejected.

## License

MIT
