ALTER TABLE review_agent.github_app_installations
    ADD COLUMN checks_permission TEXT NOT NULL DEFAULT 'none'
        CHECK (checks_permission IN ('none', 'read', 'write'));
