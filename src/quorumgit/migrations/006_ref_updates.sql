-- Bind a push's pre-receive validation to Git's own reference transaction.
-- pre-receive records each exact ref update it validated, with the paths it
-- derived from the received objects (against every ref for protected paths,
-- against the mainline for claim scopes). The reference-transaction hook
-- re-validates it and consumes its approvals while Git holds the ref locks
-- ("prepared"), then records whether Git committed or aborted the update. An
-- approval is therefore never spent on an update that did not happen, and
-- ownership is re-checked at the last abortable moment.

CREATE TABLE ref_updates (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    refname TEXT NOT NULL CHECK (refname <> ''),
    oldrev TEXT NOT NULL CHECK (
        length(oldrev) BETWEEN 40 AND 64
        AND oldrev NOT GLOB '*[^0-9a-f]*'
    ),
    newrev TEXT NOT NULL CHECK (
        length(newrev) BETWEEN 40 AND 64
        AND newrev NOT GLOB '*[^0-9a-f]*'
    ),
    pusher_agent_id INTEGER NOT NULL REFERENCES agents(id),
    paths TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(paths) AND json_type(paths) = 'array'
    ),
    scope_paths TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(scope_paths) AND json_type(scope_paths) = 'array'
    ),
    status TEXT NOT NULL DEFAULT 'validated'
        CHECK (status IN ('validated', 'prepared', 'committed', 'aborted')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    resolved_at INTEGER,
    CHECK (status IN ('validated', 'prepared') OR resolved_at IS NOT NULL)
);

-- quorumgit-statement
CREATE INDEX ref_updates_in_flight
    ON ref_updates(repository_id, refname, newrev)
    WHERE status IN ('validated', 'prepared');

-- quorumgit-statement
-- One update can need several approvals (a protected ref, out-of-scope
-- paths, protected paths). Each consumed at `prepared` is linked here so an
-- abort can return every one of them.
CREATE TABLE ref_update_approvals (
    ref_update_id INTEGER NOT NULL REFERENCES ref_updates(id),
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    PRIMARY KEY (ref_update_id, approval_id)
);
