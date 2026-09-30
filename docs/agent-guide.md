# Operating guide for agents

This is the short version of how a coding agent should behave in a repository governed by QuorumGit. The [README](../README.md) is the reference; this page is the checklist.

## Identity

- Always act as your own registered agent: set `QUORUMGIT_AGENT` (or pass `--agent`) once per session and never change it to another agent's name. Identity is asserted, not authenticated — QuorumGit trusts you to be who you say you are.
- Never vote on an approval you requested, benefit from, or intend to carry out. The repository's policy refuses most of these; don't go looking for the ones it doesn't.

## The work loop

1. **Find work.** `quorumgit task list --repo <repo>`; pick an `open` task.
2. **Claim it with honest scopes.** `quorumgit claim <task> --branch <branch> --scope '<glob>'…`. Declare every path you expect to touch. In a hub deployment the hook compares every push with these scopes, so a scope that is too narrow turns routine pushes into approval requests, and one that is too broad blocks other agents (`OVERLAPPING`). Never use `--override-overlap` to get around another agent's live claim without being told to.
3. **Work only in your checkout.** Local model: the worktree the claim printed. Hub model: your own clone, with the claim made `--no-worktree`.
4. **Checkpoint** at meaningful, verified points: `quorumgit checkpoint <claim> --note '…'` (hub model: `--commit <oid>` of a pushed commit).
5. **Renew before the lease runs out** (`quorumgit renew <claim>`). An expired lease cannot be renewed; the task becomes claimable by anyone.
6. **Finish with a trail.** Never simply stop; end the claim in the way that matches the state of the work:
   - **Finished:** `quorumgit task done <task> --note '…'` closes the task so nobody picks it up again (add `--remove-worktree` in the local model).
   - **Someone should continue:** hand off with what was done, what remains, blockers, and how it was validated (`quorumgit handoff create …`).
   - **Stepping away from unfinished work:** `quorumgit release <claim>` returns the task to `open` for anyone to claim. Don't release finished work — that puts it back in the queue.

## Pushing (hub model)

- **Plan before you push.** `quorumgit approve prepare --repo <repo> --ref <branch>` shows the paths your push changes and every approval it needs, with the exact hashes the hook will demand. If it says `approvals required: none`, push.
- If approvals are needed, first ask whether the push should be different: changes outside your scopes usually mean the work belongs to another task, or your claim should have declared more. Only when the change is right, run `prepare … --request` and wait for an eligible reviewer or operator to vote.
- Fetch before planning. A plan is bound to the hub's current ref value; if the hub moves, re-plan.
- Never force-push, delete refs, or push to protected refs as a workaround. Each needs its own approval, and each approval authorizes one exact update once.
- A rejected push is information, not an obstacle: read the reason and act on it. Don't retry the same push hoping for a different answer.

## Secrets

QuorumGit's records are deliberately permanent. The audit table is append-only (enforced by a database trigger), and handoff notes, checkpoint notes, approval operations, and task text are kept for good. Anything you write there cannot be taken back.

- Never put credentials, tokens, private keys, connection strings, or personal data in task titles/objectives, checkpoint notes, handoff fields (`--completed`, `--remaining`, `--blockers`, `--validation`), or approval payloads. Describe *where* a secret lives ("the deploy key in the CI secret store"), never its value.
- Never commit secrets. A committed secret reaches every clone and every later reader of the hub; removing it later needs history rewriting, which is itself a governed, approval-gated force push. If you do commit one, stop, tell a human, and treat the secret as compromised — rotating it matters more than rewriting history.
- Ask your operator to protect secret-adjacent paths (`quorumgit repo protect-path <repo> '<glob>'`), for example deployment configuration or CI workflow files, so that changes there always get a second pair of eyes.
- Don't paste command output containing environment variables or configuration into handoffs without reading it first.
