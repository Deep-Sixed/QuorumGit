-- Bind a push's pre-receive validation to Git's own reference transaction.
-- pre-receive records each exact ref update it validated. The
-- reference-transaction hook re-validates it and consumes any approval while
-- Git holds the ref locks ("prepared"), then records whether Git committed or
-- aborted the update. An approval is therefore never spent on an update that
-- did not happen, and ownership is re-checked at the last abortable moment.

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
    operation TEXT CHECK (
        operation IS NULL
        OR (json_valid(operation) AND json_type(operation) = 'object')
    ),
    approval_id INTEGER REFERENCES approvals(id),
    status TEXT NOT NULL DEFAULT 'validated'
        CHECK (status IN ('validated', 'prepared', 'committed', 'aborted')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    resolved_at INTEGER,
    CHECK (status IN ('validated', 'aborted') OR operation IS NULL OR approval_id IS NOT NULL),
    CHECK (status IN ('validated', 'prepared') OR resolved_at IS NOT NULL)
);

-- quorumgit-statement
CREATE INDEX ref_updates_in_flight
    ON ref_updates(repository_id, refname, newrev)
    WHERE status IN ('validated', 'prepared');
