# Third-party guidance

This review procedure, the profile's `SOUL.md`, and its workspace `AGENTS.md`
adapt guidance from two MIT-licensed projects. The upstream skills are not
installed: the managed reviewer has no skill loader, so only the adapted text
reaches the model.

To update an adaptation, review the upstream change at a new exact commit,
revise the adapted text, and run the profile and worker tests plus the normal
bundle checks. Do not fetch skills at runtime.

## Ponytail

Source: https://github.com/DietrichGebert/ponytail, release v4.7.0 (commit
adad50d), retrieved 2026-06-22. Its simplicity rules are adapted into the
"Ponytail lite" guidance in `SOUL.md` and the remediation guidance of this
procedure.

```text
MIT License

Copyright (c) 2026 Dietrich Gebert

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Cloudflare security-audit

Source:
https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8/skills/security-audit,
retrieved 2026-09-15. Its guidance mode and relevant attack classes are adapted
into the "Cloudflare security audit guidance" section of `SKILL.md`. The
adaptation keeps source-first boundary tracing, adversarial validation,
impact-based prioritization, and minimal remediation. Review Agent keeps its own
diff scope, severity, finding schema, coverage accounting, and publication. The
upstream six-phase audit, verifier agents, sandboxed execution, coverage ledger,
and report artifacts are not used, and no upstream code runs during review.

```text
MIT License

Copyright (c) 2025-2026 Cloudflare, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
