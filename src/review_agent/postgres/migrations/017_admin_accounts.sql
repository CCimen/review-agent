CREATE TABLE review_agent.admin_users (
    id UUID PRIMARY KEY,
    email VARCHAR(320) NOT NULL,
    hashed_password VARCHAR(1024) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_superuser BOOLEAN NOT NULL DEFAULT false,
    is_verified BOOLEAN NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX admin_users_email_idx ON review_agent.admin_users (lower(email));

CREATE TABLE review_agent.admin_sessions (
    token VARCHAR(43) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES review_agent.admin_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX admin_sessions_user_idx ON review_agent.admin_sessions (user_id);
CREATE INDEX admin_sessions_created_idx ON review_agent.admin_sessions (created_at);
