-- Approval authority: who may authorize is decided by the repository, not by
-- the agent asking for permission.
--
-- Agents carry one role. Every repository owns its approval policy: the
-- threshold, the roles whose votes count, and whether the requester may vote.
-- Existing agents become workers, so no historical identity silently gains
-- authority; an operator must be designated explicitly after upgrading.

ALTER TABLE agents
    ADD COLUMN role TEXT NOT NULL DEFAULT 'worker'
        CHECK (role IN ('worker', 'reviewer', 'operator'));

-- quorumgit-statement
ALTER TABLE repositories
    ADD COLUMN approval_threshold INTEGER NOT NULL DEFAULT 1
        CHECK (approval_threshold >= 1);

-- quorumgit-statement
ALTER TABLE repositories
    ADD COLUMN requester_may_vote INTEGER NOT NULL DEFAULT 0
        CHECK (requester_may_vote IN (0, 1));

-- quorumgit-statement
CREATE TABLE repository_approval_roles (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('worker', 'reviewer', 'operator')),
    UNIQUE (repository_id, role)
);

-- quorumgit-statement
INSERT INTO repository_approval_roles (repository_id, role)
SELECT id, 'operator' FROM repositories;

-- quorumgit-statement
CREATE TRIGGER repositories_default_approval_roles
AFTER INSERT ON repositories
BEGIN
    INSERT INTO repository_approval_roles (repository_id, role)
    VALUES (NEW.id, 'operator');
END;

-- quorumgit-statement
ALTER TABLE approvals
    ADD COLUMN repository_id INTEGER REFERENCES repositories(id);

-- quorumgit-statement
UPDATE approvals
SET repository_id = (
    SELECT r.id FROM repositories r
    WHERE r.name = json_extract(approvals.operation, '$.repository')
)
WHERE repository_id IS NULL;

-- quorumgit-statement
CREATE TRIGGER approvals_require_registered_repository
BEFORE INSERT ON approvals
WHEN NEW.repository_id IS NULL
  OR NOT EXISTS (
      SELECT 1 FROM repositories
      WHERE id = NEW.repository_id
        AND name = json_extract(NEW.operation, '$.repository')
  )
BEGIN
    SELECT RAISE(ABORT, 'approval must name a registered repository');
END;

-- quorumgit-statement
CREATE TRIGGER votes_require_eligible_voter
BEFORE INSERT ON votes
WHEN NOT EXISTS (
    SELECT 1
    FROM approvals ap
    JOIN repositories r ON r.id = ap.repository_id
    JOIN agents v ON v.id = NEW.voter_agent_id
    JOIN repository_approval_roles rr
        ON rr.repository_id = r.id AND rr.role = v.role
    WHERE ap.id = NEW.approval_id
      AND (r.requester_may_vote = 1
           OR ap.requested_by_agent_id IS NOT NEW.voter_agent_id)
)
BEGIN
    SELECT RAISE(ABORT, 'approval voter is not eligible under repository policy');
END;

-- quorumgit-statement
CREATE TRIGGER vote_updates_require_eligible_voter
BEFORE UPDATE OF vote, voter, voter_agent_id ON votes
WHEN NOT EXISTS (
    SELECT 1
    FROM approvals ap
    JOIN repositories r ON r.id = ap.repository_id
    JOIN agents v ON v.id = NEW.voter_agent_id
    JOIN repository_approval_roles rr
        ON rr.repository_id = r.id AND rr.role = v.role
    WHERE ap.id = NEW.approval_id
      AND (r.requester_may_vote = 1
           OR ap.requested_by_agent_id IS NOT NEW.voter_agent_id)
)
BEGIN
    SELECT RAISE(ABORT, 'approval voter is not eligible under repository policy');
END;

-- quorumgit-statement
CREATE TRIGGER approvals_consumer_is_not_approver
BEFORE UPDATE OF status ON approvals
WHEN NEW.status = 'consumed'
  AND EXISTS (
      SELECT 1 FROM votes
      WHERE approval_id = NEW.id
        AND voter_agent_id = NEW.consumed_by_agent_id
        AND vote = 1
  )
BEGIN
    SELECT RAISE(ABORT, 'an approver cannot consume its own approval');
END;
