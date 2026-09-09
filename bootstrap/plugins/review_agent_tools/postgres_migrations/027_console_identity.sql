ALTER TABLE review_agent.admin_users
    ADD COLUMN oidc_issuer TEXT,
    ADD COLUMN oidc_subject VARCHAR(255),
    ADD COLUMN scim_issuer TEXT,
    ADD COLUMN scim_external_id VARCHAR(255),
    ADD COLUMN scim_deleted BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN scim_created_at TIMESTAMPTZ,
    ADD COLUMN scim_modified_at TIMESTAMPTZ,
    ADD CONSTRAINT admin_users_oidc_identity_check CHECK (
        (oidc_issuer IS NULL) = (oidc_subject IS NULL)
    );
CREATE UNIQUE INDEX admin_users_oidc_identity_idx
    ON review_agent.admin_users (oidc_issuer, oidc_subject)
    WHERE oidc_issuer IS NOT NULL;
CREATE UNIQUE INDEX admin_users_scim_external_id_idx
    ON review_agent.admin_users (scim_issuer, scim_external_id)
    WHERE scim_external_id IS NOT NULL;
CREATE INDEX admin_users_scim_page_idx
    ON review_agent.admin_users (scim_issuer, id)
    WHERE NOT scim_deleted;

CREATE TABLE review_agent.admin_oidc_requests (
    state_digest VARCHAR(64) PRIMARY KEY,
    browser_digest VARCHAR(64) NOT NULL,
    issuer TEXT NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    nonce VARCHAR(43) NOT NULL,
    code_verifier VARCHAR(86) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    link_user_id UUID REFERENCES review_agent.admin_users(id) ON DELETE CASCADE,
    link_access_revision BIGINT,
    link_session_token VARCHAR(43) REFERENCES review_agent.admin_sessions(token) ON DELETE CASCADE
);
CREATE INDEX admin_oidc_requests_expiry_idx ON review_agent.admin_oidc_requests (expires_at);
CREATE INDEX admin_oidc_requests_browser_idx ON review_agent.admin_oidc_requests (browser_digest);
CREATE INDEX admin_oidc_requests_session_idx ON review_agent.admin_oidc_requests (link_session_token);
