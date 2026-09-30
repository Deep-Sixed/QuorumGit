-- Record the claim-scope path list alongside the protected-path list.
-- Claim scopes are measured against the mainline (the default branch and the
-- protected refs), protected paths against every ref, so a validated update
-- carries both and the reference-transaction `prepared` stage replays both.
-- NULL marks an update validated before this migration; it is recomputed.

ALTER TABLE ref_updates ADD COLUMN scope_paths TEXT CHECK (
    scope_paths IS NULL
    OR (json_valid(scope_paths) AND json_type(scope_paths) = 'array')
);
