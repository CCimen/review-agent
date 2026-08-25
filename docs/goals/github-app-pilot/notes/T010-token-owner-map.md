# Repository-scoped token owner map

One Review-Agent-specific internal module should mint short-lived GitHub App
installation tokens. PostgreSQL installation and repository-access state is the
authorization owner; the caller must bind a stable provider repository ID to a
currently active installation before signing or exchanging credentials.

The App private key stays in the admission/worker trust domain and is never
mounted into Hermes. Hermes receives only a short-lived token scoped to the
one repository and the minimum permissions needed for the current operation.
Source reads and publication keep their existing GitHub adapters; they change
how they obtain a token rather than gaining a second transport implementation.

The next design must handle installation-token expiry, permission reduction,
repository removal during a lease, and fork pull requests explicitly. GitHub's
installation token does not support the `/user` identity endpoint, so existing
code must not assume every token represents a user. No generic provider or
credential framework is justified for this pilot.
