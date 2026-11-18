ALTER TABLE review_agent.admin_users
    ADD COLUMN is_global_viewer BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN is_platform_owner BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN access_revision BIGINT NOT NULL DEFAULT 0;
UPDATE review_agent.admin_users SET is_platform_owner = true WHERE is_superuser;
ALTER TABLE review_agent.admin_users
    ADD CONSTRAINT admin_users_owner_requires_admin CHECK (NOT is_platform_owner OR is_superuser),
    ADD CONSTRAINT admin_users_global_viewer_exclusive CHECK (NOT (is_global_viewer AND is_superuser));

-- Retain existing deployment-wide read access. Newly created users receive
-- team access only unless an administrator explicitly grants a global role.
UPDATE review_agent.admin_users SET is_global_viewer = true WHERE NOT is_superuser;

CREATE TABLE review_agent.teams (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE UNIQUE INDEX teams_name_idx ON review_agent.teams (lower(name));

CREATE TABLE review_agent.team_members (
    team_id BIGINT NOT NULL REFERENCES review_agent.teams(id),
    user_id UUID NOT NULL REFERENCES review_agent.admin_users(id),
    role TEXT NOT NULL CHECK (role IN ('maintainer', 'viewer')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX team_members_user_idx ON review_agent.team_members (user_id, team_id);

CREATE TABLE review_agent.team_repositories (
    repository_id BIGINT PRIMARY KEY REFERENCES review_agent.repositories(id),
    team_id BIGINT NOT NULL REFERENCES review_agent.teams(id),
    assigned_by UUID NOT NULL REFERENCES review_agent.admin_users(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX team_repositories_team_idx ON review_agent.team_repositories (team_id, repository_id);

CREATE TABLE review_agent.repository_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id BIGINT NOT NULL REFERENCES review_agent.teams(id),
    requester_id UUID NOT NULL REFERENCES review_agent.admin_users(id),
    repository_name TEXT NOT NULL CHECK (length(repository_name) BETWEEN 3 AND 200),
    repository_id BIGINT REFERENCES review_agent.repositories(id),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    decided_by UUID REFERENCES review_agent.admin_users(id),
    decided_at TIMESTAMPTZ,
    decision_reason TEXT CHECK (length(btrim(decision_reason)) BETWEEN 1 AND 500),
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL AND decision_reason IS NULL)),
    CHECK (status <> 'approved' OR repository_id IS NOT NULL)
);
CREATE UNIQUE INDEX repository_requests_pending_idx
    ON review_agent.repository_requests (team_id, lower(repository_name)) WHERE status = 'pending';
CREATE INDEX repository_requests_team_idx ON review_agent.repository_requests (team_id, id DESC);
CREATE INDEX repository_requests_status_idx ON review_agent.repository_requests (status, id DESC);

CREATE TABLE review_agent.admin_audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id BIGINT REFERENCES review_agent.teams(id),
    actor_id UUID REFERENCES review_agent.admin_users(id),
    actor_role TEXT NOT NULL CHECK (length(actor_role) BETWEEN 1 AND 40),
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 80),
    subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2000),
    details JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 16000),
    owner_only BOOLEAN NOT NULL DEFAULT false,
    operation_id UUID,
    outcome TEXT NOT NULL DEFAULT 'succeeded' CHECK (outcome IN ('started', 'succeeded', 'failed')),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX admin_audit_events_team_idx ON review_agent.admin_audit_events (team_id, id DESC);
CREATE INDEX admin_audit_events_actor_idx ON review_agent.admin_audit_events (actor_id, id DESC);
CREATE INDEX admin_audit_events_operation_idx ON review_agent.admin_audit_events (operation_id, id) WHERE operation_id IS NOT NULL;
