# QuorumGit

**Git governance for multiple coding agents sharing one repository.**

QuorumGit prevents coding agents (or humans) working the same codebase from stepping on each other: claiming the same task twice, editing the same checkout, touching overlapping paths, abandoning work without a trail, or force-pushing over someone else's branch. It is a CLI and an embedded database — no daemon, no server, no background process — and every rule it enforces leaves an append-only audit record.

```
agent-one ──┐
agent-two ──┼──► quorumgit CLI ──► local SQLite file (claims, leases, approvals, audit)
reviewer  ──┘                          │
                                       └──► git pre-receive + reference-transaction hooks (push enforcement)
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

Eight concepts, in the order you meet them:

| Concept | What it is |
|---|---|
| **Repository** | A registered Git repo that QuorumGit governs. |
| **Agent** | A registered identity with one role — `worker` (the default), `reviewer`, or `operator`. Set via `QUORUMGIT_AGENT` or `--agent`. |
| **Task** | A unit of work against one repository. Its holder closes it with `task done`; a done task cannot be claimed until an operator reopens it. |
| **Claim** | An agent's exclusive lease on a task: names a branch, declares at least one write **scope** (path glob), and expires at a timestamp (default lease: 8 hours, set with `--lease-hours`). Expired leases make the task reclaimable and cannot be renewed — evaluated at read time, no timers. |
| **Worktree** | An isolated `git worktree` created per claim. Agents never share a mutable checkout; Git itself refuses to check one branch out twice. A claim that supersedes an earlier claim on the same task and branch continues its retained checkout instead of creating a second one. |
| **Handoff** | A structured continuation record (done / remaining / exact commit / blockers) that transfers work to a successor instead of abandoning it. |
| **Approval** | A sign-off by eligible agents, hash-bound to one exact operation (a specific push, takeover, or deletion), consumed on use. |
| **Approval policy** | Owned by each repository: how many votes an approval needs, which roles may cast them, and whether the requester may vote — as a default, optionally overridden per operation type (a force push can need more votes than a takeover). The agent asking for permission never chooses its own quorum. |
| **Content governance** | In the hub model, what a push *actually changes* is derived from the Git objects it carries and checked against the pushing claim's scopes and the repository's protected paths. |

Everything an agent does — claim, renew, checkpoint, hand off, release — writes an audit event in the same database transaction. The audit table is append-only, enforced by a trigger.

## Installation

Requirements: **Python 3.11–3.14**, **Git 2.28 or newer** (the first release with the `reference-transaction` hook that hub push enforcement relies on; the local model alone needs only 2.17, for `git worktree remove`). There is no database to install, no server to run, and no extension to provision: the store is a single SQLite 3 database file under `QUORUMGIT_DATA_DIR`, accessed through Python's standard-library [`sqlite3`](https://docs.python.org/3/library/sqlite3.html) module. QuorumGit has no third-party runtime dependencies. The Python interpreter must be linked against SQLite 3.38 or newer (every supported CPython release from python.org, Homebrew, and current Linux distributions is); `quorumgit` refuses to open the store otherwise.

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
migrations applied: ['001_core.sql', '002_approval_identities.sql', '004_approval_authority.sql', '005_content_governance.sql', '006_ref_updates.sql', '007_ref_update_scope_paths.sql', '008_operation_policies.sql', '009_approval_quorum.sql', '012_protected_fields.sql']
contract: ok
```

`init` creates the state directory, applies migrations, and verifies the runtime contract: required migrations, tables, and governance triggers, foreign keys on, WAL journal mode, and **every schema object the governance rules depend on**. The expected schema is derived by replaying the bundled migrations in memory, so a store whose triggers or unique indexes are missing or altered — or that carries extra indexes or triggers, or was migrated by a newer version — fails the contract instead of silently losing an invariant. Every command runs this check. It is idempotent — re-run it any time.

`quorumgit status` shows the store path, contract state, and row counts. If the store is missing or incomplete, every command exits non-zero: there is **one storage backend and no fallback**, by design.

## Quick start (five minutes)

Register a repository and two agents, then run one full work cycle:

```bash
quorumgit repo add myproject /path/to/working-repo
quorumgit agent add lead --role operator   # the first operator needs no sponsor
quorumgit agent add agent-one
quorumgit agent add agent-two
export QUORUMGIT_AGENT=agent-one

quorumgit task add --repo myproject --title "implement feature X"
# task 1 created.

quorumgit claim 1 --branch feat/x --scope 'src/**'
# claim 1 acquired (CLEAR).
# worktree: ~/.quorumgit/worktrees/repo-1/claim-1-3f9a0c1e
# branch: feat/x
```

Managed worktree paths are built from database IDs plus a random suffix. Repository and agent names are display identities and never become path components, so no registered name can place a checkout outside `QUORUMGIT_DATA_DIR/worktrees`.

The agent now works **inside that worktree** — edits, commits, tests — without touching anyone else's checkout. Along the way:

```bash
quorumgit checkpoint 1 --note "parser done, tests green"
# checkpoint 1 at <commit-oid>.
```

When the agent stops working, either release:

```bash
quorumgit release 1 --remove-worktree
```

Releasing returns the task to `open`, so any agent can claim it again — release means "I am no longer working on this". When the work is finished, the claim holder closes the task instead:

```bash
quorumgit task done 1 --note "merged in #42" --remove-worktree
```

`task done` releases the claim and moves the task to the terminal `done` status in one transaction; no one can claim it afterwards unless an operator runs `quorumgit task reopen <task> --reason <r>`, which returns it to `open` and audits the reason. Only the agent holding the task's live, unexpired claim can run it (accept an open handoff first). `--remove-worktree` uses the same non-forced removal as `release`, so a worktree with uncommitted changes refuses the whole command and nothing changes.

…or hand the work to someone else with everything they need to continue:

```bash
quorumgit handoff create 1 \
    --completed "parser implemented, unit tests pass" \
    --remaining "wire into CLI, integration tests" \
    --to agent-two

QUORUMGIT_AGENT=agent-two quorumgit handoff accept 1
# claim 2 acquired via handoff.
# worktree: ~/.quorumgit/worktrees/repo-1/claim-1-3f9a0c1e
# branch: feat/x
# continue from commit: <commit-oid>
# remaining work: wire into CLI, integration tests
```

Creating a handoff releases the original claim but keeps its worktree; accepting transfers that same worktree to the new claim. The handoff records `--last-commit` as the continuation point when given (any commit spec, stored as the full OID), otherwise the active worktree's `HEAD`; a claim without an active worktree (hub model, or a worktree removed by `doctor --repair`) must pass `--last-commit`.

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

Scopes are normalized when declared (`./src/**`, `/src/**`, `src//**` and `src\**` all become `src/**`; scopes that escape the repository root are refused). Scope overlap then uses a conservative literal-prefix test (the prefix of one glob up to its first wildcard against the other's), compared case-insensitively — it errs toward flagging.

## Two deployment models

Where the registered repository sits determines how enforcement works. Pick one per repository; don't mix them.

**Local model** — the registered repo is a working checkout on the same machine. Claims create isolated worktrees; agents commit directly in them. Coordination is entirely claim-based; no pushes, no hook needed. This is the default and the simplest setup — the quick start above is the local model.

**Hub model** — the registered repo is a **bare** hub that agents push to from their own clones. Enforcement moves to a pair of Git hooks, `pre-receive` and `reference-transaction`:

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

Owning the branch is necessary but not sufficient: when the branch has a live claim, every commit the push introduces must only touch paths inside that claim's scopes, or carry an `out_of_scope_push` approval for exactly the paths outside them (see [Content governance](#content-governance)). In the local model agents commit directly in their worktree, so no hook runs and scopes remain coordination metadata.

The hook learns who is pushing from `QUORUMGIT_AGENT` in its own environment, which Git passes through only when the remote is a **local path** (as `origin` is above). Over SSH or HTTP the variable does not reach the hook, so every push is rejected as unidentified. The hook also opens the store under its own `QUORUMGIT_DATA_DIR`, so pushers must use the same data directory as the rest of the deployment.

**Authorization is bound to the ref update itself.** `pre-receive` checks the push and records each exact ref update it accepted — with the paths it derived and the approvals it needs — but spends nothing. Git's `reference-transaction` hook then runs at the `prepared` stage, after Git has locked the refs and while it can still abort, re-checks every rule against the store's current state using the paths `pre-receive` recorded, and only then consumes the approvals. If ownership changed after `pre-receive` (a release, takeover, or handoff), the update is aborted. If Git refuses the update after `pre-receive` (another hook, a lost ref lock), no approval is spent; if Git aborts after `prepared`, the consumed approvals are restored. The `committed`/`aborted` outcome of every governed update is recorded. `pre-receive` refuses pushes when the transaction hook is missing, modified, or not executable. A transaction that carries an agent identity must match a fresh `pre-receive` validation (and a store that has vanished fails it closed); only unidentified ref transactions — local maintenance in the hub — pass through.

Accepting a handoff in the hub model prints `worktree: (none — create manually)`: there is no managed worktree to transfer, so the successor fetches the branch into its own clone and continues from the reported commit.

Checkpoints in the hub model take an explicit `--commit <oid>` (any commit spec Git resolves — a full or abbreviated OID or a ref — recorded as the full OID). Continuation points are **verified, not trusted**: the commit must exist in the registered repository, and when the claimed branch exists, be reachable from it. A typo'd or fabricated OID is rejected.

## Content governance

A claim's scopes say what an agent *intends* to modify. In the hub model the pre-receive hook also checks what the push *actually* modifies, derived only from the Git objects being received:

```
push → identify pusher → branch reservations → allowed ref namespace
     → derive changed paths from pushed objects
     → compare with the claim's scopes and the repository's protected paths
     → ALLOW, or REQUIRE the matching approval(s)
```

- **Changed paths** are the paths introduced by the commits a push brings in; a merge contributes only the paths it changes relative to *every* parent (conflict resolutions and hand edits), root commits are compared with the empty tree, and deletions carry no content. Which commits count as "brought in" depends on the check:
  - For **protected paths**, commits reachable from no existing ref. Every push to every ref is checked against protected paths, so content already in the hub was governed when it first arrived.
  - For a **claim's scopes**, commits not reachable from the branch's previous value, the hub's default branch (`HEAD`), or a protected ref. Merging `main` into a feature branch therefore does not attribute `main`'s changes to the pusher, but merging another agent's branch does, and so does pushing a commit to an unclaimed branch first and then to the claimed one: unclaimed branches carry no scopes, so their content is not treated as already governed.
- **Out-of-scope changes need an approval, not a new claim.** When a claimed branch receives changes to paths none of its scopes cover, the push requires an `out_of_scope_push` approval naming the claim and exactly those paths. Scope globs use `**` (any number of directories), `*` and `?` (within one path segment), and `[...]`; a glob with no wildcard, or ending in `/`, covers a file or everything beneath a directory. Branches with no live claim carry no scopes, so only the other checks apply to them.
- **Protected paths** need a `protected_path_update` approval whenever a push changes them, on any ref and whoever owns the branch:

  ```bash
  quorumgit repo protect-path myproject '.github/workflows/**' --agent lead
  quorumgit repo protect-path myproject 'migrations/**' --agent lead
  quorumgit repo add other /path/to/other.git --protected-path 'infrastructure/**'
  ```
- **Protected fields** (structured rules) govern one value inside JSON or TOML files rather than the whole file. A push needs a `protected_field_update` approval only when it changes that value; other edits to the same file are ordinary content. The value is named by a [JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) (`''` for the whole document), and the format comes from the glob's extension unless `--format` says otherwise:

  ```bash
  quorumgit repo protect-field myproject config/state.json /max_depth --agent lead
  quorumgit repo protect-field myproject 'services/*/settings.toml' /limits/replicas --agent lead
  quorumgit repo protect-field myproject config/state /schema_version --format json --agent lead
  ```

  Values are compared per new commit, like paths: a commit changes a value when it differs from the value in every parent, so a clean merge changes nothing and a change reverted later in the same push still counts. Values compare by JSON type and content, so `1`, `1.0`, and `true` differ while reordered object keys do not. A value appearing or disappearing is a change, and content that cannot be parsed or read counts as a change (rules fail closed).
- **Allowed ref namespaces.** Every repository accepts pushes to `refs/heads/` only. Tags, notes, and custom refs are refused until an operator opts in: `quorumgit repo allow-ref myproject refs/tags/ --agent lead` (`--remove` withdraws it). Branch claims, reservations, and scopes apply to `refs/heads/`; protected refs and protected paths apply to every allowed namespace.

One push can need several approvals — a direct push to protected `main` that edits a protected path needs both `protected_ref_update` and `protected_path_update`. They are all-or-nothing: the hook lists every missing approval at once, and consumes none unless all are usable.

### Planning a push: `approve prepare`

Rather than pushing to learn what governance requires, ask first. `approve prepare` reads objects from your clone and ref state from the registered repository and runs the **same derivation the hook uses**, so the operations and hashes it prints are exactly the ones the hook will demand:

```bash
$ quorumgit approve prepare --repo myproject --ref feat/api          # --new defaults to HEAD
repository: myproject
refname: refs/heads/feat/api
oldrev: 8e72…
newrev: a31f…
claim: 3 by agent-one (scopes: src/api/**)
changed paths (3):
  infrastructure/prod.tf
  src/api/routes.py
  src/database/schema.py
approvals required (1):
  out_of_scope_push sha256:5c1d…
    infrastructure/prod.tf
    src/database/schema.py
re-run with --request to open these approvals.
```

`--request` opens an approval for each required operation (as `--agent`/`QUORUMGIT_AGENT`), `--json` prints the plan for tooling, `--delete` plans a ref deletion, and `-C <path>` names a clone other than the current directory. Nothing is pushed or changed without `--request`. It exits non-zero when the push would be refused outright (outside the allowed namespaces, a frozen branch, or a branch claimed by someone else). Fetch first: the plan is bound to the hub's current ref values, so `prepare` refuses to plan from a clone missing any commit a hub ref points at (a single-branch clone, say), and a plan made before the hub moved simply won't match.

## Protected operations and approvals

Updates to protected refs, force pushes, ref deletions, out-of-scope pushes, protected-path changes, and lease takeovers all require an approval. An approval is bound by SHA-256 hash to the **exact operation payload** (canonical JSON) — approving one push authorizes that push at those exact revisions, and nothing else.

The flow, driven by the rejection messages themselves:

```bash
# 1. An agent pushes to a protected ref; the hook rejects and prints the operation hash:
#    protected_ref_update on refs/heads/main requires an approval
#    bound to this exact update (hash sha256:ab12…).

# 2. The agent requests that exact operation (`quorumgit approve prepare --repo myproject
#    --ref main --request` does this for every operation the push needs); an operator
#    votes on the returned approval instance ID. The threshold comes from the policy:
QUORUMGIT_AGENT=agent-one quorumgit approve request '{"type":"protected_ref_update","repository":"myproject","refname":"refs/heads/main","oldrev":"<old>","newrev":"<new>"}'
# approval 17 hash=sha256:ab12… status=pending threshold=1
quorumgit approve vote 17 --agent lead

# 3. The same push now lands. Pushing it again — or replaying the approval — is rejected:
#    the approval was consumed while Git held the ref lock, as the update committed.
```

Rules that hold no matter what:

- **An approval never bypasses branch reservations.** A claimed branch still accepts pushes only from its claiming agent; a branch frozen by an open handoff accepts none at all. The hook checks reservations *before* approvals.
- **Denial has precedence and is terminal.** One `--deny` vote denies the approval; later yes votes are refused.
- **Consumption is single-use under concurrency.** Two simultaneous pushes racing for one approval produce exactly one accepted push — the loser is rejected, not silently allowed.
- **Votes bind to one approval instance.** A delayed vote for an older denied or consumed instance cannot decide a newer request with the same operation hash. Requesters, voters, consumers, and pushers must name registered agents.
- **A consumed approval is spent, not blacklisted.** Consumption moves the approval to a terminal `consumed` state (with `consumed_at`) rather than reusing `denied`, and only one *live* (`pending` or `approved`) approval may exist per operation hash. The same operation can therefore be requested and approved again later as a new approval instance — which matters for takeovers, whose payload is stable and legitimately repeatable. What is never possible is one approval authorizing twice.
- **Nobody authorizes themselves.** Only agents whose role the repository's policy names may vote, in either direction. The requester may not vote unless the policy says so, the agent a takeover would benefit may not vote on it, and an agent that voted to approve an operation can never be the one that carries it out — pushes and takeovers by an approver are rejected, and a database trigger refuses the consumption even for writers that bypass the CLI.
- **Authority is checked when it is used, not only when it is granted.** Consumption re-derives eligible votes from current roles and policy, so raising the threshold or demoting an approver invalidates approvals that no longer meet it. Such an approval is reopened by the next vote on it, so eligible agents can bring it up to the new requirement instead of requesting it again.
- **The repository owns the policy.** New repositories require one vote from an `operator`, and the requester may not vote. Change it with `quorumgit repo policy <repo> [--threshold <n>] [--role <role>]… [--requester-may-vote | --no-requester-may-vote] [--quorum | --no-quorum]`. `approve request` no longer accepts `--threshold`.
- **Quorum mode scales the threshold with the approvers.** With `--quorum` (on the default or on one operation type's override), an operation needs **2/3 + 1 of the agents eligible to approve it** — agents holding an approving role, minus the requester (unless the policy lets it vote) and a takeover's beneficiary — and never fewer than `--threshold`. For example, with four operators a worker's request needs 3 of the 4, while an operator's own request is decided by the other three and needs all 3 of them. Because eligibility is counted when the approval is used, adding approvers raises the bar for approvals not yet used, just as raising `--threshold` does.
- **Policy can differ per operation type.** A repository's policy is its default; it may override the threshold, the approving roles, whether the requester may vote, or quorum mode for any of `protected_ref_update`, `force_update`, `ref_delete`, `out_of_scope_push`, `protected_path_update`, `protected_field_update`, and `lease_takeover`. Fields an override leaves unset keep following the default, and `--inherit` removes the override. The effective policy is enforced by the same database triggers, and re-derived at consumption like any other policy change.

  ```bash
  quorumgit repo policy myproject --operation force_update --threshold 2 --agent lead
  quorumgit repo policy myproject --operation ref_delete --threshold 2 --agent lead
  quorumgit repo policy myproject --operation out_of_scope_push --role reviewer --role operator --agent lead
  quorumgit repo policy myproject                                        # default plus every override
  quorumgit repo policy myproject --operation force_update --inherit --agent lead
  ```

### Roles and administration

Registering a `reviewer` or `operator`, changing a role (`quorumgit agent role <name> <role>`), and changing repository policy are themselves authority decisions, so each requires an operator acting through `--agent` or `QUORUMGIT_AGENT`, and each is audited. The single exception is bootstrap: while no operator exists, the first one can be designated by anyone. The last remaining operator cannot be demoted, so a store never falls back into bootstrap mode by accident.

> **Upgrading to protected fields.** Migration `012_protected_fields.sql` adds no rules, so nothing changes until an operator adds one with `quorumgit repo protect-field`.

> **Upgrading to per-operation policy.** Migration `008_operation_policies.sql` adds no overrides, so every operation keeps following the repository default until an operator sets one.

> **Upgrading to content governance.** Migration `005_content_governance.sql` restricts every existing repository to `refs/heads/`. If your agents push tags or other refs, allow those namespaces with `quorumgit repo allow-ref` after running `quorumgit init`. Claimed branches now also enforce their scopes at push time, so a claim declared too narrowly will ask for `out_of_scope_push` approvals.

> **Upgrading.** Migration `004_approval_authority.sql` makes every existing agent a `worker` and gives every existing repository the default policy. Approvals granted under the old rules (including self-approvals) stop authorizing anything until an eligible operator votes on a fresh request. After running `quorumgit init`, designate an operator with `quorumgit agent role <name> operator`.

Takeovers follow the same pattern: claiming a task someone else holds (`claim <task> --takeover`) prints the takeover operation to approve. Its payload includes the incumbent claim ID, so an unused approval cannot displace a later claim by the same agent. The takeover is atomic — the incumbent is released, the replacement claim created, and the approval consumed in one transaction, or none of it happens. A refused takeover leaves the incumbent untouched and the approval unconsumed.

The incumbent's checkout is not duplicated. When a claim supersedes an earlier claim on the same task and branch — an approved takeover, a reclaim after lease expiry, or a re-claim after `release` without `--remove-worktree` — the retained worktree, including any uncommitted work, is verified against its recorded repository and branch and transferred to the new claim (audited as `worktree.continued`). A retained checkout that has drifted to another branch or belongs to a different task is never adopted; the claim is refused with an explanation and rolls back entirely. A recorded checkout whose directory is gone has nothing to continue, so a fresh worktree is created and `quorumgit doctor` reports the stale record.

## Command reference

| Command | Purpose |
|---|---|
| `quorumgit init` | Start/provision the store (idempotent) |
| `quorumgit status` | Store health, contract check, row counts |
| `quorumgit doctor [--repair]` | Detect and conservatively reconcile managed-worktree drift and stuck ref updates |
| `quorumgit destroy --yes` | Delete the database file (managed worktrees under `QUORUMGIT_DATA_DIR/worktrees` are left in place) |
| `quorumgit repo add <name> <path> [--protected-ref <ref>]… [--protected-path <glob>]…` | Register a repository |
| `quorumgit agent add <name> [--role worker\|reviewer\|operator]` | Register an agent identity (non-workers need an operator once one exists) |
| `quorumgit agent role <name> <role>` | Change an agent's role (operator only, after bootstrap) |
| `quorumgit repo policy <name> [--operation <type> [--inherit]] [--threshold <n>] [--role <role>]… [--[no-]requester-may-vote] [--[no-]quorum]` | Show or change a repository's default approval policy, or its override for one operation type (changes are operator only); also shows protected refs, protected paths, and allowed ref namespaces |
| `quorumgit repo protect-path <name> <glob> [--remove]` | Require an approval for any push changing matching paths (operator only) |
| `quorumgit repo protect-field <name> <glob> <pointer> [--format json\|toml] [--remove]` | Require an approval for any push changing one value inside matching JSON/TOML files (operator only) |
| `quorumgit repo allow-ref <name> <prefix> [--remove]` | Allow pushes to a ref namespace such as `refs/tags/` (operator only) |
| `quorumgit task add --repo <name> --title <t> [--objective <o>]` | Create a task |
| `quorumgit task done <task> [--note <n>] [--remove-worktree]` | Close a task — live claim holder only; releases the claim |
| `quorumgit task reopen <task> --reason <r>` | Return a done task to `open` (operator only) |
| `quorumgit claim <task> --branch <b> --scope <glob>… [--no-worktree] [--takeover] [--override-overlap] [--lease-hours <h>]` | Claim a task |
| `quorumgit renew <claim> [--lease-hours <h>]` | Extend a live, unexpired lease; expired claims must be acquired again |
| `quorumgit checkpoint <claim> [--commit <commit>] [--note <n>]` | Record verified progress |
| `quorumgit release <claim> [--remove-worktree] [--reason <r>]` | Release a claim; the task returns to `open` |
| `quorumgit handoff create <claim> --completed <c> --remaining <r> [--to <agent>] [--files-changed <f>]… [--blockers <b>]… [--validation <v>] [--last-commit <commit>]` | Hand work off (from `--last-commit` when given, else the active worktree's HEAD) |
| `quorumgit handoff accept <id> [--lease-hours <h>]` | Continue handed-off work (addressee, or anyone if unaddressed) |
| `quorumgit handoff decline <id>` | Decline — addressee only; removes the retained worktree, and is refused if it has uncommitted changes |
| `quorumgit handoff cancel <id>` | Cancel — creator only; removes the retained worktree, and is refused if it has uncommitted changes |
| `quorumgit handoff list [--status <s>] / show <id>` | Inspect handoffs |
| `quorumgit approve request <json>` | Open an approval for an exact operation, at the repository's threshold |
| `quorumgit approve vote <approval-id> [--deny]` | Vote on one approval instance |
| `quorumgit approve prepare --repo <name> --ref <ref> [--new <rev> \| --delete] [-C <clone>] [--request] [--json]` | Show every approval a push would need, with the hook's exact hashes; `--request` opens them |
| `quorumgit approve hash <json>` | Compute an operation's hash |
| `quorumgit hook install --repo <name>` | Install the pre-receive and reference-transaction hooks (hub model) |
| `quorumgit audit [--entity <e>] [--entity-id <id>] [--limit <n>]` | Read the audit trail |

`repo list`, `agent list`, and `task list [--repo <name>]` enumerate what's registered. Commands that act as an agent (`claim`, `renew`, `release`, `task done`, `checkpoint`, `handoff create/accept/decline/cancel`, `approve request/vote`, `approve prepare`, and the operator actions `task reopen`, `agent add`, `agent role`, `repo policy`, `repo protect-path`, `repo protect-field`, `repo allow-ref`) also take `--agent <name>`, which overrides `QUORUMGIT_AGENT`. At least one `--scope` is required to claim. Exit codes: `0` success, `1` refused/violation/error, `2` usage error.

`quorumgit doctor` only checks worktree paths already recorded by QuorumGit. It reports:

- `missing` — recorded as active, but the directory is gone. `--repair` prunes Git's stale worktree metadata and marks the record removed.
- `orphaned` — the claim is released with no open handoff, but the checkout remains. `--repair` removes it.
- `unexpectedly_present` — recorded as removed, but the directory exists again. `--repair` removes it.
- `repository_mismatch`, `branch_mismatch`, `detached_head`, `unverifiable_checkout` — the checkout at the recorded path no longer matches the recorded repository and branch. These are never repaired automatically; inspect them by hand.

Repairs use ordinary non-forced `git worktree remove`, so dirty worktrees are reported instead of destroyed.

`doctor` also reports governed ref updates stuck in `prepared` for more than five minutes — ones whose `committed`/`aborted` hook never recorded an outcome (a killed process, an unreachable store). It reads the outcome from the ref itself: at the update's new value (or gone, for a deletion) means Git committed; still at the old value means Git aborted, and `--repair` restores the approvals consumed for it. A ref that has since moved elsewhere, or is still locked by Git, is reported for manual inspection and never guessed at.

`doctor` exits non-zero while any finding remains unresolved.

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
- The hooks govern `git push` only. Direct ref manipulation inside a repository bypasses them.
- A claim's scopes are measured against the mainline: the default branch and the protected refs. Content that reached the mainline is not charged to a claim that later merges it, so if the default branch is not protected, a change pushed there directly (subject to protected paths) is never checked against any claim. Protect the default branch.
- Approval hashes provide exact-payload binding and tamper-evidence, not approver authentication.
- Roles separate duties between cooperating agents. Because identity is asserted, a process willing to impersonate an operator can still do so; role separation stops an agent from authorizing its own work by accident or by default, not a determined impersonator.

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

Supported interpreters: Python 3.11 through 3.14; `ruff` and `pyright` target the 3.11 floor so post-3.11 syntax and APIs fail the checks. CI runs `ruff check` once on Linux, and `pyright` and the test suite on Linux, macOS, and Windows with Python 3.11 and 3.14. `ruff` is pinned to an exact version in the `dev` extra because new ruff releases enable new rules; bump it deliberately and fix any new findings in the same change. Tests run against a real SQLite database and real Git repositories — nothing is mocked. The suite covers the full acceptance workflow (register → claim → isolate → block overlap → parallel work → checkpoint → handoff → accept → approval-gated takeover → audit → restart survival), real-push hook enforcement, concurrent voting/consumption/resolution races, and a branding gate that keeps application code free of any identity other than QuorumGit.

Operating guidance for agents (the work loop, pushing through the hook, and keeping secrets out of the permanent record): [`docs/agent-guide.md`](docs/agent-guide.md).

Design notes: [`docs/parallel-code-review.md`](docs/parallel-code-review.md) records which ideas from the Parallel Code project were adopted (the checkout-identity checks in `doctor`) and which were rejected.

## License

MIT
