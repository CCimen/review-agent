CREATE TABLE review_agent.integrations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    credential_sha256 TEXT NOT NULL UNIQUE CHECK (credential_sha256 ~ '^[0-9a-f]{64}$'),
    deployment_wide BOOLEAN NOT NULL DEFAULT false,
    read_review_content BOOLEAN NOT NULL DEFAULT false,
    created_by UUID NOT NULL REFERENCES review_agent.admin_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);

CREATE TABLE review_agent.integration_teams (
    integration_id BIGINT NOT NULL REFERENCES review_agent.integrations(id),
    team_id BIGINT NOT NULL REFERENCES review_agent.teams(id),
    PRIMARY KEY (integration_id, team_id)
);
