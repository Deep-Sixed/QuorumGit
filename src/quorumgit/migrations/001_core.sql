-- QuorumGit relational schema for local libSQL.
-- Each chunk between quorumgit-statement markers is executed as one statement.
-- Governance-critical fields are relational; JSON is reserved for extensible
-- detail payloads and is validated with SQLite's native JSON functions.

CREATE TABLE repositories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name <> ''),
    path TEXT NOT NULL UNIQUE CHECK (path <> ''),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TABLE protected_refs (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    refname TEXT NOT NULL CHECK (refname <> ''),
    UNIQUE (repository_id, refname)
);

-- quorumgit-statement
CREATE TABLE agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name <> ''),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    title TEXT NOT NULL CHECK (title <> ''),
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'claimed', 'handoff', 'done', 'abandoned')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    branch TEXT NOT NULL CHECK (branch <> ''),
    acquired_at INTEGER NOT NULL DEFAULT (unixepoch()),
    lease_expires_at INTEGER NOT NULL,
    released_at INTEGER,
    release_reason TEXT
);

-- quorumgit-statement
CREATE UNIQUE INDEX claims_one_active_per_task
    ON claims(task_id) WHERE released_at IS NULL;

-- quorumgit-statement
CREATE INDEX claims_active_by_agent
    ON claims(agent_id) WHERE released_at IS NULL;

-- quorumgit-statement
CREATE TABLE scopes (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    path_glob TEXT NOT NULL CHECK (path_glob <> '')
);

-- quorumgit-statement
CREATE TABLE worktrees (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id),
    path TEXT NOT NULL UNIQUE,
    branch TEXT NOT NULL CHECK (branch <> ''),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    removed_at INTEGER
);

-- quorumgit-statement
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    commit_oid TEXT NOT NULL CHECK (
        length(commit_oid) BETWEEN 40 AND 64
        AND commit_oid NOT GLOB '*[^0-9a-f]*'
    ),
    note TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(detail) AND json_type(detail) = 'object'
    ),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TABLE handoffs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    from_claim_id INTEGER NOT NULL REFERENCES claims(id),
    from_agent_id INTEGER NOT NULL REFERENCES agents(id),
    to_agent_id INTEGER REFERENCES agents(id),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'accepted', 'declined', 'cancelled')),
    completed TEXT NOT NULL CHECK (completed <> ''),
    remaining TEXT NOT NULL CHECK (remaining <> ''),
    last_commit TEXT NOT NULL CHECK (
        length(last_commit) BETWEEN 40 AND 64
        AND last_commit NOT GLOB '*[^0-9a-f]*'
    ),
    files_changed TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(files_changed) AND json_type(files_changed) = 'array'
    ),
    blockers TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(blockers) AND json_type(blockers) = 'array'
    ),
    validation TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    resolved_at INTEGER
);

-- quorumgit-statement
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY,
    operation_hash TEXT NOT NULL UNIQUE CHECK (
        length(operation_hash) = 71
        AND substr(operation_hash, 1, 7) = 'sha256:'
        AND substr(operation_hash, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    operation TEXT NOT NULL CHECK (
        json_valid(operation)
        AND json_type(operation) = 'object'
        AND typeof(json_extract(operation, '$.type')) = 'text'
        AND length(json_extract(operation, '$.type')) > 0
        AND typeof(json_extract(operation, '$.repository')) = 'text'
        AND length(json_extract(operation, '$.repository')) > 0
    ),
    threshold INTEGER NOT NULL DEFAULT 1 CHECK (threshold >= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    decided_at INTEGER
);

-- quorumgit-statement
CREATE TABLE votes (
    id INTEGER PRIMARY KEY,
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    voter TEXT NOT NULL CHECK (voter <> ''),
    vote INTEGER NOT NULL CHECK (vote IN (0, 1)),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (approval_id, voter)
);

-- quorumgit-statement
CREATE TABLE conflict_events (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    task_id INTEGER REFERENCES tasks(id),
    agent_id INTEGER REFERENCES agents(id),
    classification TEXT NOT NULL CHECK (
        classification IN ('CLEAR', 'RELATED', 'OVERLAPPING', 'CONFLICTING', 'BLOCKED')
    ),
    detail TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(detail) AND json_type(detail) = 'object'
    ),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    entity TEXT NOT NULL CHECK (entity <> ''),
    entity_id INTEGER,
    agent TEXT,
    detail TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(detail) AND json_type(detail) = 'object'
    ),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- quorumgit-statement
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

-- quorumgit-statement
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
