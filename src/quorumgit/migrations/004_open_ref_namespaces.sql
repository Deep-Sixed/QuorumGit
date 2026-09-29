-- Ref namespaces outside refs/heads/ that a repository opts out of approval.
-- By default every non-branch ref update (tags, notes, custom refs) needs an
-- approval bound to the exact update.

CREATE TABLE open_ref_namespaces (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL CHECK (
        prefix GLOB 'refs/?*/'
        AND prefix <> 'refs/heads/'
        AND substr(prefix, 1, 11) <> 'refs/heads/'
    ),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (repository_id, prefix)
);
