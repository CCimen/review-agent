ALTER TABLE review_agent.publications
    ADD COLUMN supersession_token uuid,
    ADD COLUMN supersession_max_comment_bytes integer,
    ADD CONSTRAINT publication_supersession_authority CHECK (
        (supersession_token IS NULL AND supersession_max_comment_bytes IS NULL) OR
        (supersession_token IS NOT NULL AND supersession_max_comment_bytes IS NOT NULL
         AND supersession_max_comment_bytes > 0 AND superseded_by_publication_id IS NOT NULL)
    );
