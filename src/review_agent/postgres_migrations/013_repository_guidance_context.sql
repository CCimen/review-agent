CREATE TABLE review_agent.review_guidance_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_run_id BIGINT NOT NULL,
    base_sha TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    config_path TEXT NOT NULL,
    config_hash TEXT,
    snapshot_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    instructions_present BOOLEAN NOT NULL,
    context_file_count INTEGER NOT NULL,
    failure_code TEXT,
    loaded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT review_guidance_snapshots_run_fk
        FOREIGN KEY (review_run_id) REFERENCES review_agent.review_runs(id),
    CONSTRAINT review_guidance_snapshots_run_uk UNIQUE (review_run_id),
    CONSTRAINT review_guidance_snapshots_schema_ck CHECK (schema_version = 1),
    CONSTRAINT review_guidance_snapshots_status_ck
        CHECK (
            status IN (
                'not_configured', 'disabled', 'loaded', 'unavailable', 'invalid'
            )
        ),
    CONSTRAINT review_guidance_snapshots_path_ck
        CHECK (config_path = '.review-agent/config.toml'),
    CONSTRAINT review_guidance_snapshots_hashes_ck
        CHECK (
            base_sha ~ '^[0-9a-f]{40,64}$'
            AND (config_hash IS NULL OR config_hash ~ '^sha256:[0-9a-f]{64}$')
            AND snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    CONSTRAINT review_guidance_snapshots_payload_ck
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 524288
        ),
    CONSTRAINT review_guidance_snapshots_count_ck
        CHECK (context_file_count BETWEEN 0 AND 10),
    CONSTRAINT review_guidance_snapshots_lifecycle_ck
        CHECK (
            (
                status = 'loaded'
                AND config_hash IS NOT NULL
                AND failure_code IS NULL
            )
            OR (
                status = 'disabled'
                AND config_hash IS NOT NULL
                AND instructions_present = false
                AND context_file_count = 0
                AND failure_code IS NULL
            )
            OR (
                status = 'not_configured'
                AND config_hash IS NULL
                AND instructions_present = false
                AND context_file_count = 0
                AND failure_code IS NULL
            )
            OR (
                status IN ('unavailable', 'invalid')
                AND instructions_present = false
                AND context_file_count = 0
                AND failure_code ~ '^[a-z][a-z0-9_]{0,79}$'
            )
        )
);

COMMENT ON TABLE review_agent.review_guidance_snapshots IS
    'One immutable repository guidance aggregate read from the exact base of a review run';
