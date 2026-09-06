-- Bind new approval lifecycle writes to registered agent identities while
-- retaining nullable compatibility for history created before this migration.

ALTER TABLE approvals
    ADD COLUMN requested_by_agent_id INTEGER REFERENCES agents(id);

-- quorumgit-statement
ALTER TABLE approvals
    ADD COLUMN consumed_by_agent_id INTEGER REFERENCES agents(id);

-- quorumgit-statement
ALTER TABLE votes
    ADD COLUMN voter_agent_id INTEGER REFERENCES agents(id);

-- quorumgit-statement
CREATE UNIQUE INDEX votes_one_registered_agent
    ON votes(approval_id, voter_agent_id)
    WHERE voter_agent_id IS NOT NULL;

-- quorumgit-statement
UPDATE votes
SET voter_agent_id = (
    SELECT a.id
    FROM agents a
    WHERE a.name = votes.voter
      AND EXISTS (
          SELECT 1
          FROM audit_events vote_event
          WHERE vote_event.event_type = 'approval.vote'
            AND vote_event.entity = 'approval'
            AND vote_event.entity_id = votes.approval_id
            AND vote_event.agent = votes.voter
            AND EXISTS (
                SELECT 1
                FROM audit_events registration
                WHERE registration.event_type = 'agent.registered'
                  AND registration.entity = 'agent'
                  AND registration.entity_id = a.id
                  AND registration.agent = a.name
                  AND registration.id < vote_event.id
            )
      )
)
WHERE voter_agent_id IS NULL
  AND EXISTS (SELECT 1 FROM agents WHERE name = votes.voter);

-- quorumgit-statement
UPDATE approvals
SET requested_by_agent_id = (
    SELECT a.id
    FROM audit_events e JOIN agents a ON a.name = e.agent
    WHERE e.event_type = 'approval.requested'
      AND e.entity = 'approval'
      AND e.entity_id = approvals.id
      AND EXISTS (
          SELECT 1
          FROM audit_events registration
          WHERE registration.event_type = 'agent.registered'
            AND registration.entity = 'agent'
            AND registration.entity_id = a.id
            AND registration.agent = a.name
            AND registration.id < e.id
      )
    ORDER BY e.id LIMIT 1
)
WHERE requested_by_agent_id IS NULL;

-- quorumgit-statement
UPDATE approvals
SET consumed_by_agent_id = (
    SELECT a.id
    FROM audit_events e JOIN agents a ON a.name = e.agent
    WHERE e.event_type = 'approval.consumed'
      AND e.entity = 'approval'
      AND e.entity_id = approvals.id
      AND EXISTS (
          SELECT 1
          FROM audit_events registration
          WHERE registration.event_type = 'agent.registered'
            AND registration.entity = 'agent'
            AND registration.entity_id = a.id
            AND registration.agent = a.name
            AND registration.id < e.id
      )
    ORDER BY e.id DESC LIMIT 1
)
WHERE consumed_by_agent_id IS NULL;

-- quorumgit-statement
INSERT INTO audit_events (event_type, entity, entity_id, detail)
SELECT
    'approval.identity_invalidated',
    'approval',
    approvals.id,
    json_object(
        'reason',
        CASE
            WHEN approvals.requested_by_agent_id IS NULL
                THEN 'unregistered historical requester'
            ELSE 'unregistered historical voter'
        END
    )
FROM approvals
WHERE status IN ('pending', 'approved')
  AND (
      requested_by_agent_id IS NULL
      OR EXISTS (
          SELECT 1 FROM votes
          WHERE votes.approval_id = approvals.id
            AND votes.voter_agent_id IS NULL
      )
  );

-- quorumgit-statement
UPDATE approvals
SET status = 'denied', decided_at = COALESCE(decided_at, unixepoch())
WHERE status IN ('pending', 'approved')
  AND (
      requested_by_agent_id IS NULL
      OR EXISTS (
          SELECT 1 FROM votes
          WHERE votes.approval_id = approvals.id
            AND votes.voter_agent_id IS NULL
      )
  );

-- quorumgit-statement
CREATE TRIGGER approvals_require_registered_requester
BEFORE INSERT ON approvals
WHEN NEW.requested_by_agent_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'approval requester must be a registered agent');
END;

-- quorumgit-statement
CREATE TRIGGER approval_requester_updates_require_registration
BEFORE UPDATE OF requested_by_agent_id ON approvals
WHEN NEW.requested_by_agent_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'approval requester must be a registered agent');
END;

-- quorumgit-statement
CREATE TRIGGER approvals_require_registered_consumer
BEFORE UPDATE OF status ON approvals
WHEN NEW.status = 'consumed' AND NEW.consumed_by_agent_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'approval consumer must be a registered agent');
END;

-- quorumgit-statement
CREATE TRIGGER approval_consumer_updates_require_registration
BEFORE UPDATE OF consumed_by_agent_id ON approvals
WHEN NEW.status = 'consumed' AND NEW.consumed_by_agent_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'approval consumer must be a registered agent');
END;

-- quorumgit-statement
CREATE TRIGGER votes_require_registered_voter
BEFORE INSERT ON votes
WHEN NEW.voter_agent_id IS NULL
  OR NOT EXISTS (
      SELECT 1 FROM agents
      WHERE id = NEW.voter_agent_id AND name = NEW.voter
  )
BEGIN
    SELECT RAISE(ABORT, 'approval voter must be a registered agent');
END;

-- quorumgit-statement
CREATE TRIGGER vote_updates_require_registered_voter
BEFORE UPDATE OF voter, voter_agent_id ON votes
WHEN NEW.voter_agent_id IS NULL
  OR NOT EXISTS (
      SELECT 1 FROM agents
      WHERE id = NEW.voter_agent_id AND name = NEW.voter
  )
BEGIN
    SELECT RAISE(ABORT, 'approval voter must be a registered agent');
END;
