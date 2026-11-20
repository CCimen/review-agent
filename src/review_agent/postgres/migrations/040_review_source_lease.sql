-- Private claim material lives outside job records returned to workers and operators.
CREATE TABLE review_agent.review_job_source_leases (
    job_id BIGINT PRIMARY KEY REFERENCES review_agent.review_jobs(id) ON DELETE CASCADE,
    lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
    nonce UUID NOT NULL
);
