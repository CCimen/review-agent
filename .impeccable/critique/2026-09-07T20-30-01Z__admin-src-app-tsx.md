---
target: Review history navigation and review reader
total_score: 23
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
timestamp: 2026-09-07T20-30-01Z
slug: admin-src-app-tsx
---
Method: dual-agent (A: history_ux_design · B: history_ux_detector)

Yes—clicking a review should open the actual review. The current path, “Requests → Details,” ends at execution metadata and a generic GitHub PR link. It never delivers the findings the user expects.

The calm colors and restrained styling suit an operator interface. The structure follows internal records more closely than the task of reading a review. Both Impeccable and UI/UX frontend analysis were applied. The deterministic source scan returned zero findings; the navigation problems come from screenshot and source inspection, beyond that detector’s checks.

Recommended experience

Use History → Review as the primary flow. Keep the grouped PR list, but give each entry a clear “View review” action. Show finding count and limited coverage alongside publication status before the user opens anything. Respect the active filters: “latest matching review” can differ from the latest review overall.

Open a dedicated page with a readable review body and a compact list of previous runs beside it. Selecting another run replaces the content. On mobile, give the review the full width and provide a simple run selector. A long review deserves more room than a drawer or another nested card.

Lead with outcome, findings, reviewed commit and coverage limitations. Follow with the published review text, including finding severity and file links where the stored publication contains them. Put worker attempts, heartbeat, phase and inventory under one “Execution details” disclosure. Give “Open published review on GitHub” an exact publication link; keep “Open pull request” as a separate action. A failed or superseded request should explain its outcome without pretending there is a published review.

Preserve filters and list position on Back, and give every selected review a shareable URL. The database already stores rendered publication content and publication-part identifiers. A reader is feasible, but needs an authenticated read endpoint and generated client contract. Render the stored published snapshot safely, label it as recorded publication content, and distinguish it from any subsequent GitHub edits.

Priority issues

1. [P1] The main action never reaches the review. Users take two disclosure steps and still cannot read the findings. Add the dedicated reader and exact publication link. Suggested command: $impeccable shape.
2. [P1] Diagnostics outrank results. Ten execution fields dominate the deepest view, while findings are only a count and coverage is buried. Put the review and coverage first; move operational fields into secondary disclosure. Suggested command: $impeccable distill.
3. [P2] Nested disclosure makes history hard to navigate. Multiple open requests create a long stack without a URL identifying the selected run. Replace nested expansion with a run selector and stable review routes. Suggested command: $impeccable layout.
4. [P2] The overview hides useful distinctions. “Published” is visible before finding count or limited coverage, requiring extra clicks to judge relevance. Bring those signals into the PR row. Suggested command: $impeccable clarify.

Useful additions, in order

- First: actual review content, direct GitHub publication links, visible coverage limitations, and shareable review URLs.
- Next: compact history per PR, readable durations such as “46m 24s,” and a commit comparison link between runs.
- Later: compare findings between runs only after defining reliable finding identity. A finding disappearing from a later review does not prove it was fixed.

Design health: 23/40. These are qualitative review scores, not measured usability results; 4 is strongest.

| Heuristic | Score | Evidence |
|---|---:|---|
| System status | 3/4 | Useful lifecycle states; coverage less prominent |
| Match to user expectations | 2/4 | “Details” shows execution, not the review |
| User control | 2/4 | Selected run has no dedicated URL |
| Consistency | 3/4 | Coherent controls and visual language |
| Error prevention | 3/4 | Read-only surface with explicit failure states |
| Recognition over recall | 2/4 | Users must remember runs across disclosure levels |
| Efficiency | 1/4 | Repeated expansion still does not expose findings |
| Minimalism | 2/4 | Repeated status, timestamps and metadata |
| Error recovery | 3/4 | Failure and retry states exist |
| Help and explanation | 2/4 | Internal phase and coverage terms need interpretation |
| Total | 23/40 | Usable foundation; reading workflow needs substantial improvement |

What to retain: explicit distinctions between published, superseded and failed requests, plus honest coverage warnings. Source also shows useful native controls, disclosure semantics, focus handling and reduced-motion support.

Persona checks: Alex, a frequent operator, cannot bookmark a selected run and must reopen disclosures to compare reviews. Jordan, a first-time reader, is promised review details but reaches phase, heartbeat and hashes. Sam, using keyboard or assistive technology, faces repeated disclosure controls and potentially noisy freshness announcements; that announcement behavior still needs live verification.

Minor observations: show “46m 24s” instead of “2,784 seconds”; avoid repeating refresh messages inside every PR; verify touch targets and mobile reading width. The experience starts with a clear PR overview, then becomes less reassuring as every click exposes more machinery without the expected review.

Decision to resolve before implementation: whether the next slice should deliver the complete review reader and flatter history navigation together.
