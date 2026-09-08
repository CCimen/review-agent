CREATE TABLE review_agent.model_connections (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    runtime_key TEXT NOT NULL UNIQUE CHECK (runtime_key ~ '^[a-z][a-z0-9-]{0,62}$'),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    team_id BIGINT REFERENCES review_agent.teams(id),
    state TEXT NOT NULL DEFAULT 'disabled'
        CHECK (state IN ('enabled', 'disabled', 'authenticating', 'needs_attention', 'retired')),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    allowed_routes JSONB NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(allowed_routes) = 'array' AND jsonb_array_length(allowed_routes) <= 50),
    installed_contract JSONB CHECK (jsonb_typeof(installed_contract) = 'object' AND octet_length(installed_contract::text) <= 16000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CHECK (runtime_key <> 'shared' OR (id = 1 AND team_id IS NULL))
);
CREATE INDEX model_connections_team_idx ON review_agent.model_connections (team_id, id);

-- Existing queued reviews keep their original shared runtime and account revision.
INSERT INTO review_agent.model_connections (runtime_key, name, state)
VALUES ('shared', 'Shared connection', 'enabled');

CREATE TABLE review_agent.model_accounts (
    connection_id BIGINT NOT NULL REFERENCES review_agent.model_connections(id),
    provider TEXT NOT NULL CHECK (provider IN ('openai-codex', 'anthropic')),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    label TEXT NOT NULL DEFAULT '' CHECK (length(label) <= 80),
    identity_sha256 TEXT CHECK (identity_sha256 ~ '^[0-9a-f]{64}$'),
    observed_at TIMESTAMPTZ,
    PRIMARY KEY (connection_id, provider)
);
INSERT INTO review_agent.model_accounts (connection_id, provider)
VALUES (1, 'openai-codex'), (1, 'anthropic');

CREATE TABLE review_agent.team_model_policies (
    team_id BIGINT PRIMARY KEY REFERENCES review_agent.teams(id),
    connection_id BIGINT REFERENCES review_agent.model_connections(id),
    provider TEXT CHECK (provider IN ('openai-codex', 'anthropic')),
    model TEXT CHECK (length(btrim(model)) BETWEEN 1 AND 200),
    reasoning_effort TEXT CHECK (reasoning_effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CHECK ((provider IS NULL AND model IS NULL AND reasoning_effort IS NULL)
        OR (provider IS NOT NULL AND model IS NOT NULL AND reasoning_effort IS NOT NULL))
);

-- These indexed projections have one owner: the immutable admission document.
-- They are never maintained by a second application write path.
ALTER TABLE review_agent.review_subjects
    ADD COLUMN model_connection_id BIGINT GENERATED ALWAYS AS
        (COALESCE((resolved_config #>> '{model_route,connection_id}')::bigint, 1)) STORED
        REFERENCES review_agent.model_connections(id),
    ADD COLUMN model_provider TEXT GENERATED ALWAYS AS
        (resolved_config #>> '{review_contract,model_provider}') STORED,
    ADD COLUMN model_account_revision BIGINT GENERATED ALWAYS AS
        (COALESCE((resolved_config #>> '{model_route,account_revision}')::bigint, 1)) STORED,
    ADD COLUMN admission_team_id BIGINT GENERATED ALWAYS AS
        ((resolved_config #>> '{model_route,team_id}')::bigint) STORED
        REFERENCES review_agent.teams(id);
CREATE INDEX review_subjects_model_connection_idx
    ON review_agent.review_subjects (model_connection_id, model_provider, model_account_revision, id);

CREATE TABLE review_agent.model_login_sessions (
    id UUID PRIMARY KEY,
    connection_id BIGINT NOT NULL REFERENCES review_agent.model_connections(id),
    provider TEXT NOT NULL CHECK (provider = 'openai-codex'),
    account_revision BIGINT NOT NULL CHECK (account_revision > 0),
    connection_revision BIGINT NOT NULL CHECK (connection_revision > 0),
    runtime_instance UUID NOT NULL,
    actor_id UUID NOT NULL REFERENCES review_agent.admin_users(id),
    actor_role TEXT NOT NULL,
    team_id BIGINT REFERENCES review_agent.teams(id),
    remote_session_id TEXT CHECK (remote_session_id ~ '^[A-Za-z0-9_-]{22,80}$'),
    status TEXT NOT NULL CHECK (status IN
        ('starting', 'pending', 'polling', 'cancelling', 'approved', 'denied', 'expired', 'cancelled', 'needs_attention')),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    started_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    poll_after TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (connection_id, provider) REFERENCES review_agent.model_accounts(connection_id, provider)
);
CREATE UNIQUE INDEX model_login_sessions_active_idx ON review_agent.model_login_sessions (connection_id)
    WHERE finished_at IS NULL;
CREATE INDEX model_login_sessions_actor_idx ON review_agent.model_login_sessions (actor_id, started_at DESC);

CREATE TABLE review_agent.model_executions (
    job_id BIGINT NOT NULL REFERENCES review_agent.review_jobs(id),
    lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
    connection_id BIGINT NOT NULL REFERENCES review_agent.model_connections(id),
    provider TEXT NOT NULL,
    account_revision BIGINT NOT NULL CHECK (account_revision > 0),
    runtime_instance UUID NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, lease_generation),
    FOREIGN KEY (connection_id, provider) REFERENCES review_agent.model_accounts(connection_id, provider)
);
CREATE INDEX model_executions_active_connection_idx ON review_agent.model_executions (connection_id, runtime_instance)
    WHERE finished_at IS NULL;
CREATE UNIQUE INDEX model_executions_active_job_idx ON review_agent.model_executions (job_id)
    WHERE finished_at IS NULL;
