-- QuorumGit core schema.
-- Hot/filterable fields are relational columns; full structured artifacts
-- live in JSONB validated by pg_jsonschema CHECK constraints (defense in
-- depth — the application also validates before insert and re-verifies on
-- read where hashes are involved).

CREATE TABLE repositories (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name <> ''),
    path TEXT NOT NULL UNIQUE CHECK (path <> ''),
    protected_refs TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agents (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    title TEXT NOT NULL CHECK (title <> ''),
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'claimed', 'handoff', 'done', 'abandoned')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE claims (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    branch TEXT NOT NULL CHECK (branch <> ''),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason TEXT
);

-- At most one unreleased claim per task. Lease expiry does not release a
-- claim by itself; expiry is evaluated at read time and expired claims are
-- released explicitly (with an audit trail) when superseded.
CREATE UNIQUE INDEX claims_one_active_per_task
    ON claims(task_id) WHERE released_at IS NULL;
CREATE INDEX claims_active_by_agent ON claims(agent_id) WHERE released_at IS NULL;

CREATE TABLE scopes (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    path_glob TEXT NOT NULL CHECK (path_glob <> '')
);

CREATE TABLE worktrees (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL UNIQUE REFERENCES claims(id),
    path TEXT NOT NULL UNIQUE,
    branch TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at TIMESTAMPTZ
);

CREATE TABLE checkpoints (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    commit_oid TEXT NOT NULL CHECK (commit_oid ~ '^[0-9a-f]{40,64}$'),
    note TEXT NOT NULL DEFAULT '',
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE handoffs (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    from_claim_id BIGINT NOT NULL REFERENCES claims(id),
    from_agent_id BIGINT NOT NULL REFERENCES agents(id),
    to_agent_id BIGINT REFERENCES agents(id),  -- NULL = open to any agent
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'accepted', 'declined')),
    record JSONB NOT NULL CHECK (
        jsonb_matches_schema(
            '{
                "type": "object",
                "required": ["completed", "remaining", "last_commit"],
                "properties": {
                    "completed": {"type": "string", "minLength": 1},
                    "remaining": {"type": "string", "minLength": 1},
                    "last_commit": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "blockers": {"type": "array", "items": {"type": "string"}},
                    "validation": {"type": "string"}
                }
            }'::json,
            record
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE approvals (
    id BIGSERIAL PRIMARY KEY,
    operation_hash TEXT NOT NULL UNIQUE
        CHECK (operation_hash ~ '^sha256:[a-f0-9]{64}$'),
    operation JSONB NOT NULL CHECK (
        jsonb_matches_schema(
            '{
                "type": "object",
                "required": ["type", "repository"],
                "properties": {
                    "type": {"type": "string", "minLength": 1},
                    "repository": {"type": "string", "minLength": 1}
                }
            }'::json,
            operation
        )
    ),
    threshold INT NOT NULL DEFAULT 1 CHECK (threshold >= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);

CREATE TABLE votes (
    id BIGSERIAL PRIMARY KEY,
    approval_id BIGINT NOT NULL REFERENCES approvals(id),
    voter TEXT NOT NULL CHECK (voter <> ''),
    vote BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (approval_id, voter)
);

CREATE TABLE conflict_events (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    task_id BIGINT,
    agent_id BIGINT,
    classification TEXT NOT NULL CHECK (
        classification IN ('CLEAR', 'RELATED', 'OVERLAPPING', 'CONFLICTING', 'BLOCKED')
    ),
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    entity TEXT NOT NULL CHECK (entity <> ''),
    entity_id BIGINT,
    agent TEXT,
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The audit trail is append-only at the database level.
CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
