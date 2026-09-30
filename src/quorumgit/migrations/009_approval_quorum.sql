-- Optional quorum mode for approval policy. When on, the number of eligible
-- yes votes required is 2/3 + 1 of the agents eligible to approve that
-- particular approval (never less than approval_threshold), instead of the
-- fixed approval_threshold alone.
--
-- The repository default is on or off; a per-operation override may turn it
-- on or off for one operation type, and NULL there inherits the default.

ALTER TABLE repositories
    ADD COLUMN approval_quorum INTEGER NOT NULL DEFAULT 0
        CHECK (approval_quorum IN (0, 1));

-- quorumgit-statement
ALTER TABLE operation_approval_policies
    ADD COLUMN approval_quorum INTEGER
        CHECK (approval_quorum IS NULL OR approval_quorum IN (0, 1));
