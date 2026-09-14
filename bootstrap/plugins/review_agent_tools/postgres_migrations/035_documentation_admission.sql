ALTER TABLE review_agent.github_webhook_deliveries
    ADD COLUMN review_purpose TEXT NOT NULL DEFAULT 'code' CHECK (review_purpose IN ('code', 'documentation')),
    ADD COLUMN pr_number INTEGER CHECK (pr_number > 0),
    ADD COLUMN automatic_documentation BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN coalesced_by_delivery_id BIGINT CHECK (coalesced_by_delivery_id > id),
    ADD CONSTRAINT github_webhook_documentation_projection_ck CHECK (
        NOT automatic_documentation OR (
            review_purpose = 'documentation' AND pr_number IS NOT NULL
            AND provider_repository_id IS NOT NULL AND event_name = 'pull_request'
        )
    );
CREATE INDEX github_webhook_documentation_settling_idx
    ON review_agent.github_webhook_deliveries (provider_repository_id, pr_number, id DESC)
    WHERE automatic_documentation;

CREATE TABLE review_agent.documentation_admissions (
    review_run_id BIGINT PRIMARY KEY REFERENCES review_agent.review_runs(id),
    delivery_id BIGINT NOT NULL CHECK (delivery_id > 0),
    trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('manual', 'automatic', 'check_rerun')),
    trigger_json JSONB NOT NULL CHECK (jsonb_typeof(trigger_json) = 'object' AND octet_length(trigger_json::text) <= 8192),
    policy_json JSONB NOT NULL CHECK (jsonb_typeof(policy_json) = 'object' AND octet_length(policy_json::text) <= 8192),
    admitted_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    promoted_delivery_id BIGINT CHECK (promoted_delivery_id > 0),
    promotion_json JSONB CHECK (jsonb_typeof(promotion_json) = 'object' AND octet_length(promotion_json::text) <= 8192),
    promoted_at TIMESTAMPTZ,
    CHECK ((promoted_delivery_id IS NULL) = (promoted_at IS NULL)),
    CHECK ((promoted_delivery_id IS NULL) = (promotion_json IS NULL)),
    CHECK (promoted_delivery_id IS NULL OR trigger_kind = 'automatic')
);

-- Retain the small normalized documentation envelope after coalescing so the
-- exact triggering event remains inspectable. Raw webhook bodies are not stored.
ALTER TABLE review_agent.github_webhook_deliveries
    DROP CONSTRAINT github_webhook_deliveries_lifecycle_ck;
ALTER TABLE review_agent.github_webhook_deliveries
    ADD CONSTRAINT github_webhook_deliveries_lifecycle_ck CHECK (
        (status = 'received' AND normalized_payload IS NOT NULL AND lease_owner IS NULL
         AND lease_expires_at IS NULL AND last_heartbeat_at IS NULL AND completed_by IS NULL AND processed_at IS NULL)
        OR (status = 'processing' AND normalized_payload IS NOT NULL AND lease_owner IS NOT NULL
         AND lease_expires_at IS NOT NULL AND last_heartbeat_at IS NOT NULL AND failure_code IS NULL
         AND failure_actor IS NULL AND completed_by IS NULL AND processed_at IS NULL)
        OR (status IN ('accepted','ignored','rejected','failed')
         AND (normalized_payload IS NULL OR review_purpose = 'documentation')
         AND lease_owner IS NULL AND lease_expires_at IS NULL AND last_heartbeat_at IS NULL
         AND completed_by IS NOT NULL AND processed_at IS NOT NULL
         AND ((status = 'accepted' AND failure_code IS NULL AND failure_actor IS NULL)
           OR (status IN ('ignored','rejected','failed') AND failure_code IS NOT NULL AND failure_actor IS NOT NULL)))
    );
