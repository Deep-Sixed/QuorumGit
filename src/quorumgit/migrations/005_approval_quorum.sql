-- Optional quorum mode for a repository's approval policy. When on, the
-- number of eligible yes votes required is 2/3 + 1 of the agents eligible to
-- approve that particular approval (never less than approval_threshold),
-- instead of the fixed approval_threshold alone.

ALTER TABLE repositories
    ADD COLUMN approval_quorum INTEGER NOT NULL DEFAULT 0
        CHECK (approval_quorum IN (0, 1));
