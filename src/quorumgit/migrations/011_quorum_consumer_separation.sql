-- Quorum consumers may have voted, but their own vote must never authorize
-- their own action. Fixed-threshold policy keeps the original strict
-- separation rule. Under quorum policy, consumption is allowed only when the
-- other currently eligible yes votes still satisfy the quorum computed over
-- the other currently eligible approvers.
--
-- Add the effective quorum bit to the policy view, expose the current eligible
-- voter set as a view, and tighten the engine trigger to the same rule the
-- Python gate applies.

DROP VIEW approval_effective_policy;

-- quorumgit-statement
CREATE VIEW approval_effective_policy AS
SELECT
    ap.id AS approval_id,
    COALESCE(op.requester_may_vote, r.requester_may_vote) AS requester_may_vote,
    COALESCE(op.approval_threshold, r.approval_threshold) AS approval_threshold,
    COALESCE(op.approval_quorum, r.approval_quorum) AS approval_quorum,
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
CREATE VIEW approval_eligible_agents AS
SELECT ap.id AS approval_id, a.id AS agent_id
FROM approvals ap
JOIN approval_effective_policy p ON p.approval_id = ap.id
JOIN agents a
WHERE (p.requester_may_vote = 1 OR ap.requested_by_agent_id IS NOT a.id)
  AND NOT (
      json_extract(ap.operation, '$.type') = 'lease_takeover'
      AND json_extract(ap.operation, '$.to_agent') = a.name
  )
  AND CASE WHEN p.override_has_roles
      THEN EXISTS (
          SELECT 1 FROM operation_approval_roles orr
          WHERE orr.policy_id = p.override_id AND orr.role = a.role
      )
      ELSE EXISTS (
          SELECT 1 FROM repository_approval_roles rr
          WHERE rr.repository_id = p.repository_id AND rr.role = a.role
      )
  END;

-- quorumgit-statement
DROP TRIGGER approvals_consumer_is_not_approver;

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
  AND NOT (
      (SELECT approval_quorum
       FROM approval_effective_policy
       WHERE approval_id = NEW.id) = 1
      AND
      (
          SELECT count(*)
          FROM votes v
          JOIN approval_eligible_agents e
            ON e.approval_id = v.approval_id
           AND e.agent_id = v.voter_agent_id
          WHERE v.approval_id = NEW.id
            AND v.vote = 1
            AND v.voter_agent_id IS NOT NEW.consumed_by_agent_id
      ) >= max(
          (SELECT approval_threshold
           FROM approval_effective_policy
           WHERE approval_id = NEW.id),
          (
              2 * (
                  SELECT count(*)
                  FROM approval_eligible_agents
                  WHERE approval_id = NEW.id
                    AND agent_id IS NOT NEW.consumed_by_agent_id
              )
          ) / 3 + 1
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'an approver cannot consume its own approval');
END;
