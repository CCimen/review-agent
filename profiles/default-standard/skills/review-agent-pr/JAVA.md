# Java review guidance

Use this companion only for Java code or a changed configuration consumed by a
Java module. Apply it to that module's changed behavior, not unrelated frontend,
Python, or other-language paths. AGENTS.md retains the diff, evidence, severity,
clarity, and publication gates. These are candidate checks, not required design
choices or reasons to increase the finding count.

## Establish the actual stack

Read the relevant imports, build configuration, caller, and existing owner before
using a framework rule. Account for the project's Java and framework versions,
inherited Maven parents, shared starters, generated models, validation groups,
and central service or gateway controls. An unavailable inherited or deployed
control is an evidence gap, not proof that it is absent. Do not transfer Spring
rules to Quarkus, reactive rules to a servlet flow, or rules from a newer release
to an older dependency.

Preserve established vocabulary and framework idioms. A Spring endpoint may
legitimately be named `Resource`; `var`, fluent model methods, mutable persistence
entities, checked domain exceptions, and project-specific test libraries are not
defects by themselves. Apply AGENTS.md's clarity gate to the exact interpretation
mismatch. Do not require `Controller`, records, constructor injection, Optional
return types, a package layout, or a particular member order merely because an
external style guide prefers them.

## Java behavior and clarity

- Trace an `Optional.get()`, null dereference, cast, or collection operation to
  the actual values supplied by callers. Check the existing guard and contract
  before claiming an empty value, unexpected type, or duplicate key is reachable.
- For shared mutable fields, establish the object's actual scope, concurrent
  callers, synchronization, and affected request or resource. A singleton bean
  is not a race by itself. For swallowed or wrapped errors, follow the error to
  its consumer and show lost recovery, rollback, diagnostic, or response behavior;
  do not prescribe a new exception hierarchy just for consistency.
- For asynchronous or reactive code, identify the executor or scheduler that
  actually runs the operation. Demonstrate shared-thread blocking, lost request
  context, or repeated side effects before reporting. Do not recommend blanket
  memoization, conversion to reactive APIs, or replacement of readable loops.

## Spring checks, when the module actually uses Spring

- **Proxies and transactions:** follow the actual bean and invocation path for
  method security and transactions. Verify activation, proxy or weaving mode,
  self-invocation, any enclosing transaction or authorization check, and the
  configured rollback rules. An annotation alone does not prove interception;
  self-invocation alone does not prove a failure when another effective boundary
  supplies the required control. Show the unauthorized operation or partial write.
- **Spring Security routing:** if the changed module uses Spring Security, trace
  the request through the selected filter chain, its order and `securityMatcher`,
  then the authorization matchers in that chain. Check effective fallback,
  service-level object authorization, and alternate routes. Require a reachable
  sensitive operation with an insufficient guard; missing per-controller
  annotations, an intentionally public endpoint, or no visible filter-chain bean
  do not prove an authorization bypass.
- **Tokens and browser credentials:** inspect the active decoder, validators,
  key source, authority mapping, and required issuer/audience/resource bindings.
  Do not infer validation from a token being decoded or demand JWT for a service
  using another valid authentication design. For CSRF, establish a state-changing
  browser request, automatically supplied credentials, and an accepted cross-site
  request despite the existing controls. `STATELESS` alone is not that control;
  an API requiring a non-ambient Authorization bearer token is not a CSRF finding
  merely because CSRF is disabled. Cookie and CORS recommendations need the actual
  browser, credential, origin, and integration contract.
- **Binding and validation:** trace the request value through binding, active
  validation groups, cascaded validation, shared validators, service checks, and
  persistence. Distinguish an accepted invalid value or client-controlled protected
  field from a missing preferred annotation. Check patch and bulk paths when they
  reach the same invariant. Generated models and framework defaults may already
  enforce the constraint.

## Maintainer references

These references document the checks; the managed reviewer must not fetch
external skills or documentation during a run. Verify version-specific behavior
against available project source and context; omit a candidate when the decisive
fact is unavailable.

- [Spring Security filter-chain selection](https://docs.spring.io/spring-security/reference/servlet/configuration/java.html)
- [Spring method authorization](https://docs.spring.io/spring-security/reference/servlet/authorization/method-security.html)
- [Spring proxy behavior](https://docs.spring.io/spring-framework/reference/core/aop/proxying.html)
- [Spring transaction boundaries](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html)
- [Spring CSRF and browser credentials](https://docs.spring.io/spring-security/reference/features/exploits/csrf.html)
- [Spring JWT validation](https://docs.spring.io/spring-security/reference/servlet/oauth2/resource-server/jwt.html)

The Java and Spring check topics were considered alongside ECC's
[java-coding-standards](https://github.com/affaan-m/ecc/blob/ef648e01899ba3e8dc6371642deaaf64b4477775/skills/java-coding-standards/SKILL.md)
and [springboot-security](https://github.com/affaan-m/ecc/blob/ef648e01899ba3e8dc6371642deaaf64b4477775/skills/springboot-security/SKILL.md).
This companion is selective Review Agent guidance, not an installed copy of those
skills. Cloudflare's vendored security guidance remains unchanged.
