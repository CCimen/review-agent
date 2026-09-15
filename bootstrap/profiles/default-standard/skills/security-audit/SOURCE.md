# Cloudflare security-audit source

Vendored without modification from
https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8/skills/security-audit
on 2026-09-15. The adjacent LICENSE is the repository's MIT licence at that
revision. SOURCE.md is Review Agent integration documentation.

The default-standard profile installs this source with the same integrity
receipt as the other managed skills. Installation does not enable Hermes's
native skill, delegation, shell, or filesystem tools.

Automatic `/review` runs load `review-agent-pr/SKILL.md` as their route procedure.
Its **Cloudflare security audit guidance** section adapts the upstream guidance
mode and relevant attack classes into that actual model input. This follows the
existing Ponytail integration: the reviewer does not need to discover or fetch
another skill during a run. No upstream code executes during review.

The adaptation preserves source-first boundary tracing, adversarial validation,
impact-based prioritization, and minimal remediation. Review Agent retains its
diff scope, P0–P3 severity, two passes in one model turn, finding schema, history,
coverage accounting, and deterministic publication. Unknown prerequisites do not
become published findings. The upstream six-phase audit, independent verifier
agents, sandboxed execution, coverage ledger, and report artifacts are not
implemented by this integration. Documentation review uses its own procedure.

To update, review a new exact upstream commit, replace the vendored files and
licence, review the corresponding guidance adaptation, and run the profile and
worker tests plus the normal bundle checks. Release and reinstall the managed
profile through the existing deployment process; do not fetch skills at runtime.
