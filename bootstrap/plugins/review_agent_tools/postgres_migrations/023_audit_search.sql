ALTER TABLE review_agent.admin_audit_events ADD COLUMN actor_email TEXT;

-- Older events retain the best available email alongside their stable actor ID.
-- New events capture the email when the action is recorded.
UPDATE review_agent.admin_audit_events event
SET actor_email = account.email
FROM review_agent.admin_users account WHERE account.id = event.actor_id;

ALTER TABLE review_agent.admin_audit_events ADD COLUMN search_text TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(actor_email, '') || ' ' || subject || ' ' ||
                    reason || ' ' || replace(action, '_', ' ') || ' ' || details::text)
    ) STORED;
CREATE INDEX admin_audit_events_search_idx ON review_agent.admin_audit_events USING GIN (search_text);
CREATE INDEX admin_audit_events_time_idx ON review_agent.admin_audit_events (recorded_at, id);
CREATE INDEX admin_audit_events_action_idx ON review_agent.admin_audit_events (action, id DESC);
