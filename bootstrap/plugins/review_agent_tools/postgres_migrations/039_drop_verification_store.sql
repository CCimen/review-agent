-- Verification results were never written by any service, so the readers of
-- these tables always saw nothing. The private verification-export command
-- builds a file from review data and does not use them.
DROP TABLE review_agent.candidate_reconciliations;
DROP TABLE review_agent.candidate_verifications;
DROP TABLE review_agent.verification_runs;
