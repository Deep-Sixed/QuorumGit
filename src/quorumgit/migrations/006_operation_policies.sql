-- Per-operation approval policy.
--
-- A repository's approval policy is its default. It may override the
-- threshold, the approving roles, and whether the requester may vote for one
-- operation type (a force push needing two votes where a lease takeover needs
-- one, say). A NULL column, or an override with no roles of its own, inherits
-- the repository default, so overrides only ever say what differs.

CREATE TABLE operation_approval_policies (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL CHECK (operation_type <> ''),
    approval_threshold INTEGER CHECK (approval_threshold IS NULL OR approval_threshold >= 1),
    requester_may_vote INTEGER CHECK (requester_may_vote IS NULL OR requester_may_vote IN (0, 1)),
    UNIQUE (repository_id, operation_type)
);

-- quorumgit-statement
CREATE TABLE operation_approval_roles (
    id INTEGER PRIMARY KEY,
    policy_id INTEGER NOT NULL
        REFERENCES operation_approval_policies(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('worker', 'reviewer', 'operator')),
    UNIQUE (policy_id, role)
);

-- quorumgit-statement
-- The effective policy for every approval: the override for its operation
-- type where one says something, the repository default otherwise.
CREATE VIEW approval_effective_policy AS
SELECT
    ap.id AS approval_id,
    COALESCE(op.requester_may_vote, r.requester_may_vote) AS requester_may_vote,
    COALESCE(op.approval_threshold, r.approval_threshold) AS approval_threshold,
    op.id AS override_id,
    EXISTS (
        SELECT 1 FROM operation_approval_roles orr WHERE orr.policy_id = op.id
    ) AS override_has_roles,
    r.id AS repository_id
FROM approvals ap
JOIN repositories r ON r.id = ap.repository_id
LEFT JOIN operation_approval_policies op
    ON op.repository_id = r.id
   AND op.operation_type = json_extract(ap.operation, '$.type');

-- quorumgit-statement
DROP TRIGGER votes_require_eligible_voter;

-- quorumgit-statement
CREATE TRIGGER votes_require_eligible_voter
BEFORE INSERT ON votes
WHEN NOT EXISTS (
    SELECT 1
    FROM approvals ap
    JOIN approval_effective_policy p ON p.approval_id = ap.id
    JOIN agents v ON v.id = NEW.voter_agent_id
    WHERE ap.id = NEW.approval_id
      AND (p.requester_may_vote = 1
           OR ap.requested_by_agent_id IS NOT NEW.voter_agent_id)
      AND CASE WHEN p.override_has_roles
          THEN EXISTS (SELECT 1 FROM operation_approval_roles orr
                       WHERE orr.policy_id = p.override_id AND orr.role = v.role)
          ELSE EXISTS (SELECT 1 FROM repository_approval_roles rr
                       WHERE rr.repository_id = p.repository_id AND rr.role = v.role)
      END
)
BEGIN
    SELECT RAISE(ABORT, 'approval voter is not eligible under repository policy');
END;

-- quorumgit-statement
DROP TRIGGER vote_updates_require_eligible_voter;

-- quorumgit-statement
CREATE TRIGGER vote_updates_require_eligible_voter
BEFORE UPDATE OF vote, voter, voter_agent_id ON votes
WHEN NOT EXISTS (
    SELECT 1
    FROM approvals ap
    JOIN approval_effective_policy p ON p.approval_id = ap.id
    JOIN agents v ON v.id = NEW.voter_agent_id
    WHERE ap.id = NEW.approval_id
      AND (p.requester_may_vote = 1
           OR ap.requested_by_agent_id IS NOT NEW.voter_agent_id)
      AND CASE WHEN p.override_has_roles
          THEN EXISTS (SELECT 1 FROM operation_approval_roles orr
                       WHERE orr.policy_id = p.override_id AND orr.role = v.role)
          ELSE EXISTS (SELECT 1 FROM repository_approval_roles rr
                       WHERE rr.repository_id = p.repository_id AND rr.role = v.role)
      END
)
BEGIN
    SELECT RAISE(ABORT, 'approval voter is not eligible under repository policy');
END;
