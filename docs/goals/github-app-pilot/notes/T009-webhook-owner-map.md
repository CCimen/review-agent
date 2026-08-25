# Direct webhook owner map

The existing admission server remains the only public HTTP owner. A separate
GitHub App route will verify untouched bytes and durably commit a bounded,
normalized delivery before returning `202`; the current Actions route remains
unchanged until pilot cutover.

Pure event normalization belongs beside the shared GitHub signature verifier.
A new PostgreSQL module will own transport idempotency, claims, fenced leases,
terminal outcomes, and expiry recovery. The later processor must feed the
existing run/job admission transaction rather than create another review path.

The ledger stores a delivery GUID, event/action, body digest, stable provider
IDs, bounded typed command data, lifecycle state, and timestamps. It never
stores raw webhook bytes, signatures, or complete comment bodies. A repeated
GUID with the same event and digest is idempotent; a conflicting reuse fails
closed without replacing the original.

This slice excludes GitHub network calls, App credentials, token minting,
processing into a review run, installation mutation, and public capability
claims.
