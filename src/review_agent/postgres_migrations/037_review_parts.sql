-- A code review too large for one reviewer session runs as part jobs beside
-- its main job. Part 0 is the main job that plans, reconciles, and delivers.
ALTER TABLE review_agent.review_jobs
    ADD COLUMN part INTEGER NOT NULL DEFAULT 0 CHECK (part >= 0),
    DROP CONSTRAINT review_jobs_run_uk,
    ADD CONSTRAINT review_jobs_run_part_uk UNIQUE (review_run_id, part);

-- The part that owns a changed file's diff coverage. NULL belongs to the main job.
ALTER TABLE review_agent.review_run_files
    ADD COLUMN part INTEGER CHECK (part > 0);

CREATE TABLE review_agent.review_parts (
    review_run_id BIGINT NOT NULL REFERENCES review_agent.review_runs(id),
    part INTEGER NOT NULL CHECK (part > 0),
    -- Part-job lease generation whose diff reads count as this part's coverage.
    read_generation BIGINT CHECK (read_generation > 0),
    report JSONB,
    reported_at TIMESTAMPTZ,
    -- Main-job lease generation whose final session received the report.
    report_read_generation BIGINT CHECK (report_read_generation > 0),
    PRIMARY KEY (review_run_id, part),
    CONSTRAINT review_parts_report_ck CHECK ((report IS NULL) = (reported_at IS NULL))
);
