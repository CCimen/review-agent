ALTER TABLE review_agent.model_connections
    ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 4 CHECK (max_concurrency > 0);
ALTER TABLE review_agent.team_model_policies
    ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 4 CHECK (max_concurrency > 0);
ALTER TABLE review_agent.model_accounts
    ADD COLUMN quota_observed_at TIMESTAMPTZ,
    ADD COLUMN quota_wait_until TIMESTAMPTZ;

-- Runtime dispatch order stays separate from user-edited model policy revisions.
-- NULL is the single retained-work scope for requests admitted before teams.
CREATE TABLE review_agent.review_dispatch_turns (
    team_id BIGINT UNIQUE NULLS NOT DISTINCT REFERENCES review_agent.teams(id),
    last_claimed_at TIMESTAMPTZ NOT NULL
);
