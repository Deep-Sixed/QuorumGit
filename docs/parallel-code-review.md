# Parallel Code ideas for QuorumGit

Reviewed upstream main at `3d29839bad68eeec55e5e41a1306dac6898909ca`.
Source: https://github.com/johannesjo/parallel-code
Upstream is MIT licensed, copyright 2026 Johannes Millan. This change is an
independent Python implementation of an observed design idea; no upstream code
is copied. This was a targeted source review, not a full security audit or an
execution of the Electron application.

## Adopted: inspect the actual checkout

`electron/ipc/git.ts:getWorktreeStatus` reads the current branch rather than
assuming the task's original branch is still checked out.
`electron/ipc/git-adoption.integration.test.ts` exercises branch changes and
detached HEAD with real Git. This is directly useful for QuorumGit: a directory
can exist while its checkout no longer matches recorded ownership.

Doctor now compares the checkout root, Git common directory, and full branch
ref with the recorded repository and branch. It reports mismatches and refuses
automatic repair for those records. It does not silently adopt another branch
or delete a checkout whose identity changed. This is a diagnostic improvement,
not protection against a local process changing Git state after inspection.

## Useful follow-ups

- `listImportableWorktrees` inventories Git's own worktree registrations. A
  read-only unmanaged-worktree report could find Git-success/DB-failure orphans;
  adoption must still validate claims, scopes, and handoff reservations.
- `getWorktreeStatus` distinguishes committed and uncommitted changes. A
  checkpoint readiness command could expose both without committing agent work.
- `worktree-cleanup.ts` bounds filesystem inspection and avoids following
  symlinks. Use these limits if doctor gains filesystem discovery.

## Ideas that need different governance semantics

- Automatic branch adoption must not transfer QuorumGit ownership implicitly.
- Shared symlinks to ignored dependency directories can expose mutable state
  across agents. They are not an isolation guarantee.
- UI-driven merging, forced cleanup, and Docker ownership recovery should not
  bypass exact-operation approvals or delete retained work.
- An Electron agent launcher, mobile monitor, and agent comparison UI are a
  different product scope from the current embedded governance CLI.

PR #8 builds on merged PR #7, main at `0faef92ec2170e6b5ecdca6ce7e5c318b668b581`.
