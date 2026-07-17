-- Creator-initiated cancellation is distinct from an addressee's decline.
ALTER TABLE handoffs DROP CONSTRAINT handoffs_status_check;
ALTER TABLE handoffs ADD CONSTRAINT handoffs_status_check
    CHECK (status IN ('open', 'accepted', 'declined', 'cancelled'));
