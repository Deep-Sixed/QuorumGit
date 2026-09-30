"""What a ref update actually carries, derived from Git objects alone.

Governance decisions about content must come from the objects Git is
receiving, not from what an agent said it planned to modify. Everything here
is a pure function of a Git object database: the pre-receive hook calls it
against the hub (quarantined objects included, since Git exports the
quarantine through the environment), and ``approve prepare`` calls the same
code against an agent's clone before pushing. Both therefore derive the same
changed paths, and so the same operation hashes, for the same update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitObjectError(RuntimeError):
    pass


ZERO_OID = "0" * 40


def is_zero(oid: str) -> bool:
    return set(oid) == {"0"}


def zero_oid_like(oid: str) -> str:
    """The null OID in the same object format (SHA-1 or SHA-256) as oid.

    Git spells "no object" with as many zeros as its hash has hex digits, and
    the hook receives it that way, so a planned operation must match.
    """
    return "0" * len(oid)


def _text_path(path: str) -> str:
    """A path as stable, encodable text.

    Git paths are bytes. Valid UTF-8 is kept as is; any other byte is written
    as a ``\\xNN`` escape, so the path can be hashed, stored, and shown, and
    every caller derives the same text for the same bytes.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raw = path.encode("utf-8", "surrogateescape")
        return raw.decode("utf-8", "backslashreplace")
    return path


def _git(git_dir: str | Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise GitObjectError(f"git {args[0]} failed{suffix}")
    return result.stdout


def absolute_git_dir(path: str | Path) -> Path:
    """The Git directory of a checkout, linked worktree, or bare repository."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise GitObjectError(f"Not a git repository: {path}{suffix}")
    return Path(result.stdout.strip())


def _refuse_option(value: str) -> None:
    if value.startswith("-"):
        raise GitObjectError(f"Refusing revision that looks like an option: {value!r}")


def resolve_object(git_dir: str | Path, rev: str) -> str:
    _refuse_option(rev)
    try:
        return _git(git_dir, "rev-parse", "--verify", f"{rev}^{{object}}").strip()
    except GitObjectError as exc:
        raise GitObjectError(f"Cannot resolve {rev!r} to a Git object.") from exc


def ref_value(git_dir: str | Path, refname: str) -> str:
    """The OID a ref currently names, or ZERO_OID when it does not exist."""
    _refuse_option(refname)
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "rev-parse", "--verify", "--quiet",
         refname],
        capture_output=True,
        text=True,
        check=False,
    )
    oid = result.stdout.strip()
    if result.returncode == 0 and oid:
        return oid
    if result.returncode == 1:
        return ZERO_OID
    detail = (result.stderr or result.stdout).strip()
    raise GitObjectError(f"Cannot read {refname}: {detail}")


def object_exists(git_dir: str | Path, oid: str) -> bool:
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "cat-file", "-e", oid],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def is_ancestor(git_dir: str | Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "merge-base", "--is-ancestor",
         ancestor, descendant],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise GitObjectError(
            f"Unable to determine whether {ancestor} is an ancestor of {descendant}."
        )
    return result.returncode == 0


def ref_tips(git_dir: str | Path) -> list[str]:
    """Commits every existing ref points at (annotated tags peeled).

    Inside pre-receive these are the values from *before* the push: Git
    updates no ref until every hook has accepted.
    """
    out = _git(
        git_dir,
        "for-each-ref",
        "--format=%(objecttype) %(objectname) %(*objecttype) %(*objectname)",
    )
    tips: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "commit":
            tips.add(parts[1])
        elif len(parts) == 4 and parts[2] == "commit":
            tips.add(parts[3])
    return sorted(tips)


def _commit_at(git_dir: str | Path, ref: str) -> str | None:
    """The commit a ref names (tags peeled), or None if it names none."""
    _refuse_option(ref)
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "rev-parse", "--verify", "--quiet",
         f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    oid = result.stdout.strip()
    return oid if result.returncode == 0 and oid else None


def mainline_tips(git_dir: str | Path, protected_refs: list[str]) -> list[str]:
    """Commits the default branch (HEAD) and the protected refs point at.

    This is the shared baseline a claimed branch is measured against. Other
    branches are someone's work in progress, and nothing checks the scopes of
    an unclaimed one, so content taken from them counts against the claim that
    brings it in. A ref that is missing or names no commit is skipped, which
    only shrinks the baseline and makes the scope check stricter.
    """
    tips = {oid for ref in ("HEAD", *protected_refs) if (oid := _commit_at(git_dir, ref))}
    return sorted(tips)


def new_commits(
    git_dir: str | Path, newrev: str, known_tips: list[str]
) -> list[str]:
    """Commits reachable from newrev and from none of known_tips."""
    lines = [newrev, *(f"^{tip}" for tip in known_tips)]
    out = _git(git_dir, "rev-list", "--stdin", stdin="\n".join(lines) + "\n")
    return out.split()


def commit_paths(git_dir: str | Path, commit: str) -> list[str]:
    """Paths one commit introduces.

    Root commits are compared with the empty tree. Merges use Git's combined
    diff, so a clean merge introduces nothing of its own and only paths that
    differ from every parent (conflict resolutions, evil merges) are counted.
    """
    out = _git(
        git_dir,
        "diff-tree", "-r", "-c", "--root", "--no-renames", "--name-only",
        "--no-commit-id", "-z", commit,
    )
    return [_text_path(path) for path in out.split("\0") if path]


def changed_paths(
    git_dir: str | Path, oldrev: str, newrev: str, known_tips: list[str]
) -> list[str]:
    """Paths the commits reachable from newrev but not from oldrev or
    known_tips introduce, sorted and de-duplicated.

    What counts as already known depends on the check. Protected paths are
    checked on every push to every ref, so content reachable from any
    existing ref (ref_tips) was governed when it arrived. Claim scopes are
    checked only on claimed branches, so they are measured against the
    mainline alone (mainline_tips). A deletion carries no content.
    """
    if is_zero(newrev):
        return []
    known = list(known_tips)
    if not is_zero(oldrev):
        known.append(oldrev)
    paths: set[str] = set()
    for commit in new_commits(git_dir, newrev, known):
        paths.update(commit_paths(git_dir, commit))
    return sorted(paths)


def commit_parents(git_dir: str | Path, commit: str) -> list[str]:
    """The commit's parents, in order (none for a root commit)."""
    out = _git(git_dir, "rev-list", "--parents", "-n", "1", commit)
    return out.split()[1:]


def blob_at(git_dir: str | Path, commit: str, path: str) -> bytes | None:
    """The contents of path in commit, or None when it holds no such file."""
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "cat-file", "blob", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout

