# Token minting before event processing

Direct review-event processing needs a fresh, authenticated pull-request
snapshot because the webhook ledger intentionally stores only bounded command
and identity data. Reusing the current PAT for that read would create a
transitional credential dependency. Processing only installation events first
would leave the durable queue partly consumable. The next slice therefore adds
repository-scoped token minting before the processor.

The token module is specific to Review Agent. PostgreSQL installation and
repository-access state authorizes every token return, including cache hits.
The outbound exchange names exactly one stable provider repository ID and only
the minimum review-read permissions. The App private key is parsed outside
Hermes, tokens live only in process memory, and concurrent refreshes for the
same scope must not stampede GitHub.

This slice remains dormant: it does not wire Hermes, deployment, publication,
the webhook processor, or the current PAT/Actions path. Tests use generated
keys and mocked HTTP. Live App registration, clock configuration, provider
permissions, fork behavior, deployment secrets, and pilot acceptance remain
later gates.
