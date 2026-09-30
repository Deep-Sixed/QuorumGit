---
name: steward
description: How to drive a QuorumGit pull request to mergeable — local checks to run before pushing, how to handle red CI, merge conflicts, and review comments in this repo. Use when opening, updating, or babysitting a PR here.
---

# Stewarding a QuorumGit PR

QuorumGit is a governance tool: its value is invariants that hold. Treat
every PR the same way — a push is only as good as the evidence behind it.

## Before every push

Run exactly what CI runs, from the repo root, after
`python -m pip install -e '.[dev]'`:

```bash
ruff check                     # Lint job (ruff is pinned exactly in pyproject.toml)
pyright                        # type check against the Python 3.11 floor
python -m pytest tests -q      # full suite; ~2 minutes, run it all
```

All three must be clean. Do not bump the ruff pin or loosen
`pyrightconfig.json` / `[tool.ruff]` to get green — those are the gate.

## CI

CI (`.github/workflows/ci.yml`) runs Lint plus a test matrix of
ubuntu / macos / windows × Python 3.11 / 3.14, with `fail-fast: false`.

- A failure on one OS only is a real portability bug (paths, line endings,
  file locking, SQLite version — the job prints `sqlite3.sqlite_version`).
  Fix it in code; do not mark it platform-skipped.
- A 3.14-only failure usually means a deprecation or stdlib behavior change;
  a 3.11-only failure usually means syntax or APIs above the floor.
- Never skip, xfail, or delete a test to get green, and never weaken a
  governance check (triggers, contract verification, scope/approval
  enforcement) to make a test pass. If a test is wrong, say why in the PR.

## Merge conflicts

This repo resolves conflicts by **merging `main` into the PR branch**, never
by rebasing or force-pushing (history is full of
`Merge main (#NN) into <topic>` commits). Follow that message style: name the
PRs being brought in and the topic of the branch.

Conflict hot spots:

- **Migrations** (`src/quorumgit/migrations/NNN_*.sql`). Numbers of merged
  migrations are permanent — never renumber, edit, or delete one that is on
  `main`. If a parallel PR took your number, renumber *your* unmerged
  migration to the next free slot and update every reference (the README
  `init` output, `store.py` requirements, tests). The schema contract is
  derived by replaying migrations, so re-run the full suite after.
- **`store.py` REQUIRED_TABLES / REQUIRED_TRIGGERS / REQUIRED_VIEWS**: keep
  the union of both sides.
- **README.md**: the README documents behavior and CLI output precisely;
  keep it in sync with the merged code, not just conflict-free.

## Review comments

- Address every thread: push a fix, or reply explaining why not.
- A finding that a governance rule can be bypassed (scope, approval hash
  binding, quorum, append-only audit, hook enforcement) is always blocking —
  fix it with a regression test, even if it looks unlikely.
- Bug fixes come with a test that fails before the fix.
- Keep the diff to what the PR is about; queue unrelated finds separately.

## Docs

User-visible behavior changes (CLI flags, output, policy semantics,
requirements) update `README.md` and, where agents are affected,
`docs/agent-guide.md` in the same PR.
