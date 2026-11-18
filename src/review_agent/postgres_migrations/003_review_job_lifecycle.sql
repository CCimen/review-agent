ALTER TABLE review_agent.review_jobs
    DROP CONSTRAINT review_jobs_lifecycle_ck;

ALTER TABLE review_agent.review_jobs
    ADD COLUMN failure_code TEXT;

-- Reconcile existing terminal run/job pairs before installing the stronger
-- one-to-one lifecycle invariant.
UPDATE review_agent.review_jobs AS job
SET status = CASE run.status
        WHEN 'completed' THEN 'succeeded'
        WHEN 'failed' THEN 'failed'
        WHEN 'superseded' THEN 'superseded'
        ELSE job.status
    END,
    failure_code = CASE
        WHEN run.status = 'failed' THEN 'job_run_failed'
        ELSE NULL
    END,
    lease_owner = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    completed_at = COALESCE(job.completed_at, run.completed_at, statement_timestamp())
FROM review_agent.review_runs AS run
WHERE run.id = job.review_run_id
  AND run.status IN ('completed', 'failed', 'superseded')
  AND job.status IN ('queued', 'leased');

ALTER TABLE review_agent.review_jobs
    ADD CONSTRAINT review_jobs_failure_code_ck
        CHECK (
            failure_code IS NULL
            OR (btrim(failure_code) <> '' AND char_length(failure_code) <= 80)
        ),
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
