-- A decision judges one exact occurrence; every writer already records it.
ALTER TABLE review_agent.finding_decisions
    ALTER COLUMN finding_occurrence_id SET NOT NULL;

-- A changed file's diff coverage can only belong to a part its review planned.
ALTER TABLE review_agent.review_run_files
    ADD CONSTRAINT review_run_files_part_fk
        FOREIGN KEY (review_run_id, part)
        REFERENCES review_agent.review_parts (review_run_id, part);
