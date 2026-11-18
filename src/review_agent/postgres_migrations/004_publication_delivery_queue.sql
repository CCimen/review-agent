ALTER TABLE review_agent.publications
    DROP CONSTRAINT publications_status_ck,
    DROP CONSTRAINT publications_state_timestamps_ck;

ALTER TABLE review_agent.publications
    ADD COLUMN delivery_available_at TIMESTAMPTZ,
    ADD COLUMN delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN delivery_max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN delivery_lease_owner TEXT,
    ADD COLUMN delivery_lease_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN delivery_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN delivery_last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN delivery_recovery_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN delivery_completed_at TIMESTAMPTZ;

UPDATE review_agent.publications
SET delivery_available_at = generated_at,
    delivery_attempt_count = CASE WHEN status = 'posting' THEN 1 ELSE 0 END,
    delivery_lease_owner = CASE
        WHEN status = 'posting' THEN 'migration-recovery'
        ELSE NULL
    END,
    delivery_lease_generation = CASE WHEN status = 'posting' THEN 1 ELSE 0 END,
    delivery_lease_expires_at = CASE
        WHEN status = 'posting' THEN statement_timestamp()
        ELSE NULL
    END,
    delivery_last_heartbeat_at = CASE
        WHEN status = 'posting' THEN statement_timestamp()
        ELSE NULL
    END,
    delivery_completed_at = CASE
        WHEN status IN ('posted', 'stale')
        THEN COALESCE(posted_at, publish_failed_at)
        ELSE NULL
    END;

ALTER TABLE review_agent.publications
    ALTER COLUMN delivery_available_at SET NOT NULL,
    ALTER COLUMN delivery_available_at SET DEFAULT statement_timestamp(),
    ADD CONSTRAINT publications_status_ck
        CHECK (
            status IN (
                'generated', 'posting', 'posted', 'publish_failed', 'failed', 'stale'
            )
        ),
    ADD CONSTRAINT publications_state_timestamps_ck
        CHECK (
            (
                status = 'generated' AND posting_started_at IS NULL
                AND posted_at IS NULL AND publish_failed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'posting' AND posting_started_at IS NOT NULL
                AND posted_at IS NULL AND publish_failed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'posted' AND posting_started_at IS NOT NULL
                AND posted_at IS NOT NULL
                AND publish_failed_at IS NULL AND failure_code IS NULL
            )
            OR (
                status = 'publish_failed' AND posting_started_at IS NOT NULL
                AND publish_failed_at IS NOT NULL
                AND posted_at IS NULL AND failure_code IS NOT NULL
                AND btrim(failure_code) <> ''
            )
            OR (
                status IN ('failed', 'stale')
                AND posting_started_at IS NOT NULL
                AND posted_at IS NULL AND publish_failed_at IS NOT NULL
                AND failure_code IS NOT NULL AND btrim(failure_code) <> ''
            )
        ),
    ADD CONSTRAINT publications_delivery_attempts_ck
        CHECK (
            delivery_attempt_count >= 0
            AND delivery_max_attempts > 0
            AND delivery_attempt_count <= delivery_max_attempts
            AND delivery_lease_generation >= 0
            AND delivery_recovery_count >= 0
        ),
    ADD CONSTRAINT publications_delivery_lifecycle_ck
        CHECK (
            (
                status IN ('generated', 'publish_failed')
                AND delivery_attempt_count < delivery_max_attempts
                AND delivery_lease_owner IS NULL
                AND delivery_lease_expires_at IS NULL
                AND delivery_last_heartbeat_at IS NULL
                AND delivery_completed_at IS NULL
            )
            OR (
                status = 'posting'
                AND delivery_attempt_count > 0
                AND delivery_lease_owner IS NOT NULL
                AND btrim(delivery_lease_owner) <> ''
                AND delivery_lease_generation > 0
                AND delivery_lease_expires_at IS NOT NULL
                AND delivery_last_heartbeat_at IS NOT NULL
                AND delivery_completed_at IS NULL
            )
            OR (
                status IN ('posted', 'failed', 'stale')
                AND delivery_lease_owner IS NULL
                AND delivery_lease_expires_at IS NULL
                AND delivery_last_heartbeat_at IS NULL
                AND delivery_completed_at IS NOT NULL
            )
        );

CREATE INDEX publications_delivery_claim_idx
    ON review_agent.publications (delivery_available_at, id)
    WHERE status IN ('generated', 'publish_failed');

CREATE INDEX publications_delivery_expiry_idx
    ON review_agent.publications (delivery_lease_expires_at, id)
    WHERE status = 'posting';

ALTER TABLE review_agent.review_jobs
    DROP CONSTRAINT review_jobs_lifecycle_ck;

ALTER TABLE review_agent.review_jobs
    ADD CONSTRAINT review_jobs_lifecycle_ck
        CHECK (
            (
                status = 'queued'
                AND attempt_count < max_attempts
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND last_heartbeat_at IS NULL
                AND completed_at IS NULL
            )
            OR (
                status = 'leased'
                AND lease_owner IS NOT NULL
                AND btrim(lease_owner) <> ''
                AND lease_generation > 0
                AND attempt_count > 0
                AND lease_expires_at IS NOT NULL
                AND last_heartbeat_at IS NOT NULL
                AND started_at IS NOT NULL
                AND completed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'awaiting_publication'
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND last_heartbeat_at IS NULL
                AND completed_at IS NULL
                AND failure_code IS NULL
            )
            OR (
                status IN ('succeeded', 'superseded')
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND last_heartbeat_at IS NULL
                AND completed_at IS NOT NULL
                AND failure_code IS NULL
            )
            OR (
                status IN ('failed', 'dead_letter')
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND last_heartbeat_at IS NULL
                AND completed_at IS NOT NULL
                AND failure_code IS NOT NULL
                AND btrim(failure_code) <> ''
            )
        );
