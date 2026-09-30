-- Structured protection rules: one value inside matching JSON or TOML files,
-- named by a JSON Pointer. A push needs a protected_field_update approval
-- only when it changes that value, not for every edit to the file.
--
-- A validated ref update records the field changes pre-receive derived, like
-- its paths, so the reference-transaction `prepared` stage replays exactly
-- what was approved. NULL marks an update validated before this migration;
-- it is recomputed.

CREATE TABLE protected_fields (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path_glob TEXT NOT NULL CHECK (path_glob <> ''),
    format TEXT NOT NULL CHECK (format IN ('json', 'toml')),
    pointer TEXT NOT NULL CHECK (pointer = '' OR pointer LIKE '/%'),
    UNIQUE (repository_id, path_glob, pointer)
);

-- quorumgit-statement
ALTER TABLE ref_updates ADD COLUMN field_changes TEXT CHECK (
    field_changes IS NULL
    OR (json_valid(field_changes) AND json_type(field_changes) = 'array')
);
