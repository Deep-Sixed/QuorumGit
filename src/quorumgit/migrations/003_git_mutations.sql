-- Reserve exact incoming Git mutations between pre-receive validation and
-- post-receive completion. Reservations freeze governance for the affected ref
-- while Git is in flight and bind any protected-operation approval instance to
-- that exact mutation until Git either completes or the reservation expires.

CREATE TABLE git_mutations (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    refname TEXT NOT NULL CHECK (refname <> ''),
    oldrev TEXT NOT NULL CHECK (oldrev <> ''),
    newrev TEXT NOT NULL CHECK (newrev <> ''),
    mutation_hash TEXT NOT NULL CHECK (
        length(mutation_hash) = 71
        AND substr(mutation_hash, 1, 7) = 'sha256:'
        AND substr(mutation_hash, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    approval_id INTEGER REFERENCES approvals(id),
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'completed', 'expired')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    expires_at INTEGER NOT NULL,
    completed_at INTEGER,
    CHECK (status = 'completed' OR completed_at IS NULL),
    CHECK (status <> 'completed' OR completed_at IS NOT NULL)
);

-- quorumgit-statement
CREATE UNIQUE INDEX git_mutations_one_reserved_ref
    ON git_mutations(repository_id, refname)
    WHERE status = 'reserved';

-- quorumgit-statement
CREATE UNIQUE INDEX git_mutations_one_reserved_exact
    ON git_mutations(mutation_hash)
    WHERE status = 'reserved';

-- quorumgit-statement
CREATE UNIQUE INDEX git_mutations_one_reserved_approval
    ON git_mutations(approval_id)
    WHERE status = 'reserved' AND approval_id IS NOT NULL;

-- quorumgit-statement
CREATE INDEX git_mutations_by_repository_status
    ON git_mutations(repository_id, status, expires_at);
