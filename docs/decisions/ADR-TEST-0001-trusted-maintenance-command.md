+++
id = "ADR-TEST-0001"
title = "Allow a trusted operator command in the disposable ADR validation fixture"
status = "accepted"
invariant = "The fixture receives only a preapproved maintenance command from a trusted operator; shell syntax is intentional and the fixture must never be used as an untrusted request boundary."
on_change = [
  "Keep the fixture outside production packages.",
  "Reject any caller that accepts repository, webhook, or end-user input.",
  "Remove the fixture when the ADR feedback pilot is complete."
]
evidence = "docs/FEEDBACK_AND_DECISIONS.md"
origin_pr = 3
+++

# Context

This accepted decision exists only on the disposable ADR-feedback validation
branch. It lets the pilot prove that Review Agent binds intentional feedback to
an accepted base-commit ADR and expires that decision when its evidence changes.

The branch is not intended to merge into `main`.
