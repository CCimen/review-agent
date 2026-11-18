ALTER TABLE review_agent.decision_audit
    RENAME COLUMN allowlist_version TO authorization_version;

ALTER TABLE review_agent.decision_audit
    RENAME CONSTRAINT decision_audit_allowlist_version_ck
    TO decision_audit_authorization_version_ck;
