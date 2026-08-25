CREATE TABLE review_agent.github_webhook_deliveries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_guid UUID NOT NULL,
    event_name TEXT NOT NULL,
    action TEXT,
    payload_sha256 TEXT NOT NULL,
    provider_installation_id BIGINT,
    provider_repository_id BIGINT,
    repository_full_name TEXT,
    command_category TEXT NOT NULL,
    normalized_schema_version INTEGER NOT NULL,
    normalized_payload JSONB,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    failure_code TEXT,
    failure_actor TEXT,
    completed_by TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    CONSTRAINT github_webhook_deliveries_guid_uk UNIQUE (delivery_guid),
    CONSTRAINT github_webhook_deliveries_event_ck CHECK (
        event_name ~ '^[a-z][a-z0-9_]{0,63}$'
        AND (action IS NULL OR action ~ '^[a-z][a-z0-9_]{0,63}$')
    ),
    CONSTRAINT github_webhook_deliveries_digest_ck CHECK (
        payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT github_webhook_deliveries_provider_ids_ck CHECK (
        (provider_installation_id IS NULL OR provider_installation_id > 0)
        AND (provider_repository_id IS NULL OR provider_repository_id > 0)
    ),
    CONSTRAINT github_webhook_deliveries_repository_label_ck CHECK (
        repository_full_name IS NULL
        OR (
            btrim(repository_full_name) <> ''
            AND char_length(repository_full_name) <= 255
        )
    ),
    CONSTRAINT github_webhook_deliveries_category_ck CHECK (
        command_category IN (
            'review', 'feedback', 'installation',
            'repository_access', 'ignored'
        )
    ),
    CONSTRAINT github_webhook_deliveries_normalized_payload_ck CHECK (
        normalized_schema_version > 0
        AND (
            normalized_payload IS NULL
            OR (
                jsonb_typeof(normalized_payload) = 'object'
                -- This is a storage/privacy guard, not a review-throughput cap.
                AND octet_length(normalized_payload::text) <= 1048576
            )
        )
    ),
    CONSTRAINT github_webhook_deliveries_status_ck CHECK (
        status IN (
            'received', 'processing', 'accepted',
            'ignored', 'rejected', 'failed'
        )
    ),
    CONSTRAINT github_webhook_deliveries_attempts_ck CHECK (
        max_attempts > 0
        AND attempt_count >= 0
        AND attempt_count <= max_attempts
        AND lease_generation >= 0
    ),
    CONSTRAINT github_webhook_deliveries_lease_owner_ck CHECK (
        lease_owner IS NULL
        OR (btrim(lease_owner) <> '' AND char_length(lease_owner) <= 120)
    ),
    CONSTRAINT github_webhook_deliveries_failure_ck CHECK (
        (failure_code IS NULL AND failure_actor IS NULL)
        OR (
            failure_code ~ '^[a-z][a-z0-9_]{0,63}$'
            AND btrim(failure_actor) <> ''
            AND char_length(failure_actor) <= 120
        )
    ),
    CONSTRAINT github_webhook_deliveries_completed_by_ck CHECK (
        completed_by IS NULL
        OR (btrim(completed_by) <> '' AND char_length(completed_by) <= 120)
    ),
    CONSTRAINT github_webhook_deliveries_lifecycle_ck CHECK (
        (
            status = 'received'
            AND normalized_payload IS NOT NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND last_heartbeat_at IS NULL
            AND completed_by IS NULL
            AND processed_at IS NULL
        )
        OR (
            status = 'processing'
            AND normalized_payload IS NOT NULL
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND last_heartbeat_at IS NOT NULL
            AND failure_code IS NULL
            AND failure_actor IS NULL
            AND completed_by IS NULL
            AND processed_at IS NULL
        )
        OR (
            status IN ('accepted', 'ignored', 'rejected', 'failed')
            AND normalized_payload IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND last_heartbeat_at IS NULL
            AND completed_by IS NOT NULL
            AND processed_at IS NOT NULL
            AND (
                (status = 'accepted' AND failure_code IS NULL)
                OR (status <> 'accepted' AND failure_code IS NOT NULL)
            )
        )
    ),
    CONSTRAINT github_webhook_deliveries_timestamps_ck CHECK (
        available_at >= received_at
        AND (started_at IS NULL OR started_at >= received_at)
        AND (processed_at IS NULL OR processed_at >= received_at)
        AND (
            lease_expires_at IS NULL
            OR lease_expires_at >= last_heartbeat_at
        )
    )
);

CREATE INDEX github_webhook_deliveries_ready_idx
    ON review_agent.github_webhook_deliveries (available_at, id)
    WHERE status = 'received';

CREATE INDEX github_webhook_deliveries_expired_lease_idx
    ON review_agent.github_webhook_deliveries (lease_expires_at, id)
    WHERE status = 'processing';

CREATE INDEX github_webhook_deliveries_repository_history_idx
    ON review_agent.github_webhook_deliveries (
        provider_repository_id, received_at DESC, id DESC
    )
    WHERE provider_repository_id IS NOT NULL;
