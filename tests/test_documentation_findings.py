from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"))

from review_agent_tools.documentation_findings import validate_assessment  # noqa: E402
from review_agent_tools.domain.documentation_review import (  # noqa: E402
    DocumentationResult,
    PreviousDocumentationFinding,
    decode_assessment,
    DocumentationReviewError,
    EvidenceRead,
    EvidenceRole,
)
from review_agent_tools.domain.finding import FindingContent  # noqa: E402
from review_agent_tools.domain.review import ReviewPurpose, ReviewRunId  # noqa: E402
from review_agent_tools.feedback_commands import (
    parse_review_feedback_command,
    review_command_purpose,
)  # noqa: E402
from review_agent_tools.memory_validation import ReviewMemoryError  # noqa: E402
from review_agent_tools.postgres.documentation_reviews import coverage_reasons  # noqa: E402
from review_agent_tools.repository_decision_context import not_configured  # noqa: E402
from tests.test_postgres_documentation_reviews import evidence, scope  # noqa: E402


def result_fixture() -> DocumentationResult:
    document = evidence()
    source = replace(
        document,
        path="src/api.py",
        total_lines=8,
        end_line=8,
        blob_sha="1" * 40,
        content_sha256="2" * 64,
    )
    return DocumentationResult(
        ReviewRunId(1),
        "b" * 40,
        "c" * 40,
        "a" * 40,
        scope(),
        (
            document,
            replace(document, role=EvidenceRole.COMPARISON, revision="c" * 40),
            source,
            replace(
                source,
                role=EvidenceRole.COMPARISON,
                revision="c" * 40,
                blob_sha="3" * 40,
                content_sha256="4" * 64,
            ),
        ),
        None,
        False,
        (),
        False,
        False,
    )


def input_fixture(result: DocumentationResult) -> dict[str, object]:
    citations = [
        {
            "role": read.role.value,
            "path": read.path,
            "start_line": read.start_line,
            "end_line": read.end_line,
        }
        for read in result.evidence
    ]
    content = FindingContent(
        "documentation.accuracy",
        "docs/api.md",
        3,
        "",
        "api flag behavior",
        "Explain the changed API flag",
        "Medium",
        "contracts",
        8,
        0.95,
        "The guide still promises the old flag while the source changes it.",
        "Compared both revisions; no compatibility alias remains.",
        "Readers use an unsupported flag.",
        "Replace the old flag with the new supported form.",
    )
    return {
        "findings": [
            {
                "content": asdict(content),
                "document_path": "docs/api.md",
                "area_id": "api",
                "changed_path": "src/api.py",
                "introduced_or_worsened": "The changed behavior invalidates the previously accurate guide.",
                "citations": citations,
                "adr_ids": [],
            }
        ],
        "assessments": [
            {
                "path": "docs/api.md",
                "disposition": "finding",
                "rationale": "The documented flag no longer matches the changed implementation.",
                "citations": citations,
            }
        ],
        "incomplete_reasons": [],
    }


class DocumentationFindingTests(unittest.TestCase):
    def validate(
        self,
        result: DocumentationResult,
        payload: object,
        changed=frozenset({"src/api.py"}),
    ):
        return validate_assessment(
            result,
            payload,
            changed_paths=changed,
            decisions=not_configured(base_sha=result.base_sha),
        )

    def test_previous_resolution_requires_selected_fully_assessed_document(self) -> None:
        result = result_fixture()
        prior = PreviousDocumentationFinding("F7", "1" * 64, 17, 2, "docs/api.md", "Missing guide", "Guide missing", "Add guide", "documentation.missing", "src/api.py", None, "API guide")
        payload = input_fixture(result)
        payload["findings"] = []
        payload["assessments"][0]["disposition"] = "aligned"
        citations = payload["assessments"][0]["citations"]
        payload["previous_assessments"] = [{"local_reference": "F7", "verdict": "resolved", "rationale": "The added guide now explains the flag.", "citations": citations}]
        def validate(value=result):
            return validate_assessment(value, payload, changed_paths=frozenset({"docs/api.md"}), decisions=not_configured(base_sha=value.base_sha), previous_findings=(prior,))
        receipt, definitions, reasons = validate()
        self.assertEqual(receipt.previous[0].occurrence_id, 17)
        self.assertEqual(receipt.previous[0].verdict, "resolved")
        self.assertFalse(definitions)
        self.assertFalse(reasons)
        import json
        self.assertEqual(decode_assessment(json.loads(json.dumps(receipt.to_json_obj()))), receipt)
        # The former missing-document finding is anchored in source, but resolution uses its document.
        self.assertNotEqual(prior.path, prior.document_path)
        removed = replace(result, scope=replace(result.scope, documents=(), areas=()))
        payload["assessments"] = []
        with self.assertRaisesRegex(DocumentationReviewError, "selected document"):
            validate(removed)
        payload["previous_assessments"][0]["verdict"] = "not_checked"
        payload["previous_assessments"][0]["citations"] = []
        self.assertEqual(validate(removed)[0].previous[0].verdict, "not_checked")
        payload["previous_assessments"] = []
        self.assertEqual(validate(removed)[0].previous, ())

    def test_unchanged_document_finding_is_bound_to_source_document_and_policy(
        self,
    ) -> None:
        result = result_fixture()
        receipt, definitions, reasons = self.validate(result, input_fixture(result))
        self.assertFalse(reasons)
        self.assertEqual(definitions[0].path, "docs/api.md")
        self.assertEqual(receipt.findings[0].changed_path, "src/api.py")
        original = definitions[0].context_hash
        for changed in (
            replace(
                result,
                evidence=(
                    replace(result.evidence[0], blob_sha="5" * 40),
                    *result.evidence[1:],
                ),
            ),
            replace(
                result,
                evidence=(
                    *result.evidence[:2],
                    replace(result.evidence[2], blob_sha="6" * 40),
                    result.evidence[3],
                ),
            ),
            replace(
                result, scope=replace(result.scope, policy_hash="sha256:" + "7" * 64)
            ),
        ):
            _, rechecked, _ = self.validate(changed, input_fixture(changed))
            self.assertEqual(rechecked[0].fingerprint, definitions[0].fingerprint)
            self.assertNotEqual(rechecked[0].context_hash, original)

    def test_docs_only_false_claim_and_missing_document_use_real_refs(self) -> None:
        result = result_fixture()
        changed_doc = replace(
            result,
            evidence=(
                result.evidence[0],
                replace(result.evidence[1], blob_sha="5" * 40),
                *result.evidence[2:],
            ),
        )
        payload = input_fixture(changed_doc)
        payload["findings"][0]["changed_path"] = "docs/api.md"
        self.assertEqual(
            len(self.validate(changed_doc, payload, frozenset({"docs/api.md"}))[1]), 1
        )
        for deleted in (False, True):
            absent = EvidenceRead(
                "docs/api.md",
                EvidenceRole.HEAD,
                result.head_sha,
                unavailable_reason="not_found_at_revision",
            )
            prior = (
                result.evidence[1]
                if deleted
                else replace(
                    absent, role=EvidenceRole.COMPARISON, revision=result.comparison_sha
                )
            )
            missing = replace(result, evidence=(absent, prior, *result.evidence[2:]))
            payload = input_fixture(missing)
            payload["findings"][0]["content"].update(
                rule_id="documentation.missing",
                path="docs/api.md" if deleted else "src/api.py",
            )
            self.assertEqual(len(self.validate(missing, payload)[1]), 1)

    def test_forged_citation_unchanged_cause_and_unscoped_path_are_rejected(
        self,
    ) -> None:
        result = result_fixture()
        payload = input_fixture(result)
        forged = deepcopy(payload)
        forged["findings"][0]["citations"][0]["end_line"] = 13
        with self.assertRaises(DocumentationReviewError):
            self.validate(result, forged)
        same_source = replace(
            result,
            evidence=(
                *result.evidence[:3],
                replace(result.evidence[3], blob_sha=result.evidence[2].blob_sha),
            ),
        )
        with self.assertRaisesRegex(DocumentationReviewError, "introduction"):
            self.validate(same_source, input_fixture(same_source))
        with self.assertRaisesRegex(DocumentationReviewError, "changed inventory"):
            self.validate(result, payload, frozenset())

    def test_unmapped_no_impact_and_repaired_partial_read_can_complete(self) -> None:
        result = result_fixture()
        unmapped = replace(
            result,
            scope=replace(
                result.scope, areas=(), documents=(), unmapped_paths=("src/api.py",)
            ),
        )
        payload = input_fixture(unmapped)
        payload["findings"] = []
        payload["assessments"][0].update(
            path="src/api.py",
            disposition="no_impact",
            rationale="The internal refactor preserves behavior and the public contract.",
        )
        self.assertFalse(self.validate(unmapped, payload)[2])
        partial = replace(
            result.evidence[0],
            end_line=11,
            content_sha256="5" * 64,
            unavailable_reason="partial_line",
        )
        incomplete = replace(result, evidence=(partial, *result.evidence[1:]))
        self.assertIn("partial_line", coverage_reasons(incomplete))
        self.assertNotIn(
            "partial_line",
            coverage_reasons(
                replace(incomplete, evidence=(*incomplete.evidence, result.evidence[0]))
            ),
        )

    def test_docs_feedback_prefix_never_enables_intentional_suppression(self) -> None:
        self.assertEqual(
            review_command_purpose("/REVIEW DOCS feedback nope"),
            ReviewPurpose.DOCUMENTATION,
        )
        self.assertIsNotNone(
            parse_review_feedback_command(
                "/review docs false-positive F2 because Existing guard applies."
            )
        )
        with self.assertRaises(ReviewMemoryError):
            parse_review_feedback_command(
                "/review docs intentional F2 ADR-001 because We intended this."
            )
        self.assertEqual(
            review_command_purpose("/review false-positive F2 reason"),
            ReviewPurpose.CODE,
        )

    def test_accepted_adr_metadata_invalidates_suppression_and_failed_context_stays_incomplete(
        self,
    ) -> None:
        from review_agent_tools.domain.repository_decisions import (
            DecisionIndexEntry,
            DecisionIndexMatch,
            parse_adr,
        )
        from review_agent_tools.repository_decision_context import loaded, pending

        result = result_fixture()
        payload = input_fixture(result)
        payload["findings"][0]["content"]["rule_id"] = "documentation.intent-conflict"
        payload["findings"][0]["adr_ids"] = ["ADR-0001"]
        entry = DecisionIndexEntry(
            "ADR-0001", ".review-agent/decisions/ADR-0001.md", ("src/**",)
        )
        contexts = []
        for status, invariant in (
            ("accepted", "Keep the documented contract."),
            ("accepted", "Keep the newly approved contract."),
            ("superseded", "Keep the documented contract."),
        ):
            decision = parse_adr(
                f'+++\nid = "ADR-0001"\ntitle = "API contract"\nstatus = "{status}"\ninvariant = "{invariant}"\non_change = ["Ask the owner to approve the contract change."]\n+++\n',
                match=DecisionIndexMatch(entry, 1),
            )
            contexts.append(
                loaded(
                    base_sha=result.base_sha,
                    index_hash="sha256:" + "a" * 64,
                    decisions=(decision,),
                )
            )
        first = validate_assessment(
            result,
            payload,
            changed_paths=frozenset({"src/api.py"}),
            decisions=contexts[0],
        )[1][0]
        revised = validate_assessment(
            result,
            payload,
            changed_paths=frozenset({"src/api.py"}),
            decisions=contexts[1],
        )[1][0]
        self.assertNotEqual(first.context_hash, revised.context_hash)
        with self.assertRaisesRegex(DocumentationReviewError, "accepted decision"):
            validate_assessment(
                result,
                payload,
                changed_paths=frozenset({"src/api.py"}),
                decisions=contexts[2],
            )
        reasons = validate_assessment(
            result,
            input_fixture(result),
            changed_paths=frozenset({"src/api.py"}),
            decisions=pending(base_sha=result.base_sha),
        )[2]
        self.assertTrue(any("repository decisions" in reason for reason in reasons))
