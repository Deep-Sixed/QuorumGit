-- Per-repository approval policy: an optional approver roster whose size sets
-- a 2/3 + 1 quorum, and an optional separation-of-duties rule that stops an
-- agent approving its own request or its own push/takeover.

CREATE TABLE repository_approvers (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (repository_id, agent_id)
);

-- quorumgit-statement
ALTER TABLE repositories
    ADD COLUMN separate_duties INTEGER NOT NULL DEFAULT 0
    CHECK (separate_duties IN (0, 1));
