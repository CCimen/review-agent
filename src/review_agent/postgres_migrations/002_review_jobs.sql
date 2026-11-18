CREATE TABLE review_agent.review_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    lease_owner TEXT,
    lease_generation BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT review_jobs_run_fk
        FOREIGN KEY (review_run_id) REFERENCES review_agent.review_runs(id),
    CONSTRAINT review_jobs_run_uk UNIQUE (review_run_id),
    CONSTRAINT review_jobs_attempts_ck
        CHECK (
            attempt_count >= 0
            AND max_attempts > 0
            AND attempt_count <= max_attempts
        ),
    CONSTRAINT review_jobs_lease_generation_ck CHECK (lease_generation >= 0),
    CONSTRAINT review_jobs_lifecycle_ck
        CHECK (
            (
                status = 'queued'
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
            )
            OR (
                status = 'superseded'
                AND completed_at IS NOT NULL
                AND (
                    (
                        lease_owner IS NULL
                        AND lease_expires_at IS NULL
                        AND last_heartbeat_at IS NULL
                    )
                    OR (
                        lease_owner IS NOT NULL
                        AND btrim(lease_owner) <> ''
                        AND lease_generation > 0
                        AND attempt_count > 0
                        AND lease_expires_at IS NOT NULL
                        AND last_heartbeat_at IS NOT NULL
                        AND started_at IS NOT NULL
                    )
                )
            )
        ),
    CONSTRAINT review_jobs_timestamps_ck
        CHECK (
            available_at >= created_at
            AND (started_at IS NULL OR started_at >= created_at)
            AND (completed_at IS NULL OR completed_at >= created_at)
            AND (
                last_heartbeat_at IS NULL
                OR (
                    started_at IS NOT NULL
                    AND last_heartbeat_at >= started_at
                    AND lease_expires_at > last_heartbeat_at
                )
            )
        )
);

-- enqueue_run serializes one queued job per pull request on the parent PR row.
CREATE INDEX review_jobs_claim_idx
    ON review_agent.review_jobs (priority DESC, available_at, id)
    WHERE status = 'queued';

CREATE INDEX review_jobs_lease_expiry_idx
    ON review_agent.review_jobs (lease_expires_at, id)
    WHERE status = 'leased';
