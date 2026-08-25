ALTER TABLE review_agent.review_runs
    ADD COLUMN failure_status_delivery_status TEXT NOT NULL DEFAULT 'not_required',
    ADD COLUMN failure_status_delivery_available_at TIMESTAMPTZ,
    ADD COLUMN failure_status_delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN failure_status_delivery_max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN failure_status_delivery_lease_owner TEXT,
    ADD COLUMN failure_status_delivery_lease_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN failure_status_delivery_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN failure_status_delivery_last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN failure_status_delivery_failure_code TEXT,
    ADD COLUMN failure_status_delivery_completed_at TIMESTAMPTZ;

UPDATE review_agent.review_runs
SET failure_status_delivery_status = CASE
        WHEN failure_status_comment_id IS NOT NULL THEN 'posted'
        WHEN status IN ('failed', 'superseded') THEN 'pending'
        ELSE 'not_required'
    END,
    failure_status_delivery_available_at = CASE
        WHEN status IN ('failed', 'superseded') AND failure_status_comment_id IS NULL
        THEN completed_at
        ELSE NULL
    END,
    failure_status_delivery_completed_at = CASE
        WHEN failure_status_comment_id IS NOT NULL THEN failure_status_posted_at
        ELSE NULL
    END;

ALTER TABLE review_agent.review_runs
    ADD CONSTRAINT review_runs_failure_status_delivery_status_ck CHECK (
        failure_status_delivery_status IN (
            'not_required', 'pending', 'posting', 'publish_failed',
            'posted', 'suppressed', 'failed'
        )
    ),
    ADD CONSTRAINT review_runs_failure_status_delivery_attempts_ck CHECK (
        failure_status_delivery_attempt_count >= 0
        AND failure_status_delivery_max_attempts > 0
        AND failure_status_delivery_attempt_count <= failure_status_delivery_max_attempts
        AND failure_status_delivery_lease_generation >= 0
    ),
    ADD CONSTRAINT review_runs_failure_status_delivery_lifecycle_ck CHECK (
        (failure_status_delivery_status = 'not_required'
         AND failure_status_delivery_available_at IS NULL
         AND failure_status_delivery_lease_owner IS NULL
         AND failure_status_delivery_lease_expires_at IS NULL
         AND failure_status_delivery_last_heartbeat_at IS NULL
         AND failure_status_delivery_failure_code IS NULL
         AND failure_status_delivery_completed_at IS NULL)
        OR
        (failure_status_delivery_status = 'pending'
         AND failure_status_delivery_available_at IS NOT NULL
         AND failure_status_delivery_attempt_count < failure_status_delivery_max_attempts
         AND failure_status_delivery_lease_owner IS NULL
         AND failure_status_delivery_lease_expires_at IS NULL
         AND failure_status_delivery_last_heartbeat_at IS NULL
         AND failure_status_delivery_failure_code IS NULL
         AND failure_status_delivery_completed_at IS NULL)
        OR
        (failure_status_delivery_status = 'publish_failed'
         AND failure_status_delivery_available_at IS NOT NULL
         AND failure_status_delivery_attempt_count < failure_status_delivery_max_attempts
         AND failure_status_delivery_lease_owner IS NULL
         AND failure_status_delivery_lease_expires_at IS NULL
         AND failure_status_delivery_last_heartbeat_at IS NULL
         AND failure_status_delivery_failure_code IS NOT NULL
         AND btrim(failure_status_delivery_failure_code) <> ''
         AND failure_status_delivery_completed_at IS NULL)
        OR
        (failure_status_delivery_status = 'posting'
         AND failure_status_delivery_attempt_count > 0
         AND failure_status_delivery_lease_owner IS NOT NULL
         AND btrim(failure_status_delivery_lease_owner) <> ''
         AND failure_status_delivery_lease_generation > 0
         AND failure_status_delivery_lease_expires_at IS NOT NULL
         AND failure_status_delivery_last_heartbeat_at IS NOT NULL
         AND failure_status_delivery_failure_code IS NULL
         AND failure_status_delivery_completed_at IS NULL)
        OR
        (failure_status_delivery_status IN ('posted', 'suppressed')
         AND failure_status_delivery_available_at IS NULL
         AND failure_status_delivery_lease_owner IS NULL
         AND failure_status_delivery_lease_expires_at IS NULL
         AND failure_status_delivery_last_heartbeat_at IS NULL
         AND failure_status_delivery_failure_code IS NULL
         AND failure_status_delivery_completed_at IS NOT NULL)
        OR
        (failure_status_delivery_status = 'failed'
         AND failure_status_delivery_available_at IS NULL
         AND failure_status_delivery_lease_owner IS NULL
         AND failure_status_delivery_lease_expires_at IS NULL
         AND failure_status_delivery_last_heartbeat_at IS NULL
         AND failure_status_delivery_failure_code IS NOT NULL
         AND btrim(failure_status_delivery_failure_code) <> ''
         AND failure_status_delivery_completed_at IS NOT NULL)
    ),
    ADD CONSTRAINT review_runs_failure_status_delivery_run_ck CHECK (
        (status IN ('running', 'completed')
         AND failure_status_delivery_status = 'not_required'
         AND failure_status_comment_id IS NULL
         AND failure_status_posted_at IS NULL)
        OR
        (status IN ('failed', 'superseded')
         AND failure_status_delivery_status <> 'not_required'
         AND (
            (failure_status_delivery_status = 'posted'
             AND failure_status_comment_id IS NOT NULL
             AND failure_status_posted_at IS NOT NULL)
            OR
            (failure_status_delivery_status <> 'posted'
             AND failure_status_comment_id IS NULL
             AND failure_status_posted_at IS NULL)
         ))
    );

CREATE INDEX review_runs_failure_status_delivery_claim_idx
    ON review_agent.review_runs (failure_status_delivery_available_at, id)
    WHERE failure_status_delivery_status IN ('pending', 'publish_failed');

CREATE INDEX review_runs_failure_status_delivery_expiry_idx
    ON review_agent.review_runs (failure_status_delivery_lease_expires_at, id)
    WHERE failure_status_delivery_status = 'posting';
