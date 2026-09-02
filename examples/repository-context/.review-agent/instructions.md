# Repository review instructions

## Engineering principles

- Prefer one clear owner for each domain rule and lifecycle transition.
- Favor the smallest complete long-term solution over compatibility layers or
  speculative abstractions.
- Preserve authorization, data boundaries, transaction safety, and explicit
  failure behavior.
- Keep tests focused on observable behavior and material failure modes.

## Review focus

- Prioritize correctness, reliability, maintainability, and bounded resource
  use in the changed execution path.
- Flag complexity only when it creates a concrete defect risk or near-term
  maintenance cost.

## Communication style

- Be direct, constructive, and concise.
- Explain the verified failure path and the smallest safe correction.
