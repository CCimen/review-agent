ALTER TABLE review_agent.publication_parts
    DROP CONSTRAINT publication_parts_type_ck,
    ADD CONSTRAINT publication_parts_type_ck
        CHECK (part_type IN ('summary', 'continuation', 'suggestion_review', 'check_run'));
