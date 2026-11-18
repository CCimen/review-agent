CREATE TABLE review_agent.repository_review_requesters (
    repository_id BIGINT NOT NULL REFERENCES review_agent.repositories(id),
    github_user_id BIGINT NOT NULL CHECK (github_user_id > 0),
    github_login TEXT NOT NULL CHECK (
        github_login ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}$'
    ),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (repository_id, github_user_id)
);
