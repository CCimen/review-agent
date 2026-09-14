ALTER TABLE review_agent.teams
    ADD COLUMN documentation_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (documentation_mode IN ('off', 'manual', 'automatic')),
    ADD COLUMN documentation_revision BIGINT NOT NULL DEFAULT 1
        CHECK (documentation_revision > 0);

ALTER TABLE review_agent.repositories
    ADD COLUMN documentation_mode TEXT
        CHECK (documentation_mode IN ('off', 'manual', 'automatic')),
    ADD COLUMN documentation_revision BIGINT NOT NULL DEFAULT 1
        CHECK (documentation_revision > 0);
