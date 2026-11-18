-- A single bounded display snapshot per repository. Review authority never uses it.
CREATE TABLE review_agent.repository_documentation_configuration (
    repository_id BIGINT PRIMARY KEY REFERENCES review_agent.repositories(id) ON DELETE CASCADE,
    snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(snapshot) = 'object' AND octet_length(snapshot::text) <= 262144
    ),
    refresh_started_at TIMESTAMPTZ NOT NULL
);
