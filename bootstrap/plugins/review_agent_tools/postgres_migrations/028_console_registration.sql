CREATE TABLE review_agent.admin_registration_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    enabled BOOLEAN NOT NULL DEFAULT false,
    allowed_domains JSONB NOT NULL DEFAULT '[]',
    allowed_emails JSONB NOT NULL DEFAULT '[]',
    CHECK (jsonb_typeof(allowed_domains) = 'array' AND jsonb_array_length(allowed_domains) <= 100),
    CHECK (jsonb_typeof(allowed_emails) = 'array' AND jsonb_array_length(allowed_emails) <= 100)
);
INSERT INTO review_agent.admin_registration_policy (singleton) VALUES (true);

CREATE TABLE review_agent.admin_registration_requests (
    email VARCHAR(320) PRIMARY KEY,
    token_digest CHAR(64) NOT NULL UNIQUE,
    requested_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX admin_registration_requests_expiry
    ON review_agent.admin_registration_requests (expires_at);

ALTER TABLE review_agent.admin_oidc_requests
    ADD COLUMN register_account BOOLEAN NOT NULL DEFAULT false,
    ADD CONSTRAINT admin_oidc_requests_registration_check CHECK (
        NOT register_account OR link_user_id IS NULL
    );

CREATE TABLE review_agent.admin_email_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    enabled BOOLEAN NOT NULL DEFAULT false,
    configuration JSONB,
    encrypted_password TEXT,
    last_test_at TIMESTAMPTZ,
    CHECK (NOT enabled OR configuration IS NOT NULL)
);
INSERT INTO review_agent.admin_email_settings (singleton) VALUES (true);
