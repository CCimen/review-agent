-- Keep compact, coalesced exposure ranges on the run-owned changed file.
-- Existing complete and truncated observations retain their original meaning.
ALTER TABLE review_agent.review_run_files
    ADD COLUMN diff_content_sha256 text,
    ADD COLUMN diff_total_chars bigint,
    ADD COLUMN diff_read_ranges int8multirange NOT NULL DEFAULT '{}',
    ADD CONSTRAINT review_run_files_diff_ranges_ck CHECK (
        (diff_content_sha256 IS NULL AND diff_total_chars IS NULL
            AND diff_read_ranges = '{}'::int8multirange)
        OR
        (diff_content_sha256 IS NOT NULL AND diff_total_chars IS NOT NULL
            AND diff_content_sha256 ~ '^[0-9a-f]{64}$'
            AND diff_total_chars > 0
            AND diff_read_ranges <> '{}'::int8multirange
            AND diff_read_ranges <@ int8range(0, diff_total_chars, '[)'))
    );
