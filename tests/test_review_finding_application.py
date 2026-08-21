from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import memory_db, review_finding_application  # noqa: E402


REPOSITORY = "sundsvallskommun/example-repository"
BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40


class ReviewFindingApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = os.environ.get("REVIEW_AGENT_DB")
        os.environ["REVIEW_AGENT_DB"] = str(Path(self.temp.name) / "memory.sqlite3")
        memory_db.connect(os.environ["REVIEW_AGENT_DB"]).close()
        with closing(memory_db.connect_existing()) as connection:
            run = memory_db.start_run(
                connection,
                REPOSITORY,
                1,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        self.subject = review_finding_application.FindingRecordSubject(
            repository=REPOSITORY,
            pr_number=1,
            run_id=int(run["id"]),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )

    def tearDown(self) -> None:
        if self.previous_db is None:
            os.environ.pop("REVIEW_AGENT_DB", None)
        else:
            os.environ["REVIEW_AGENT_DB"] = self.previous_db
        self.temp.cleanup()

    @staticmethod
    def finding(**overrides: object) -> dict[str, object]:
        finding: dict[str, object] = {
            "rule_id": "correctness.boolean-default",
            "category": "correctness",
            "path": "backend/changed.py",
            "line": 1,
            "symbol": "handler",
            "anchor": "feature default",
            "title": "Boolean default remains disabled",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "The feature remains unavailable.",
            "smallest_fix": "Restore the enabled default.",
            "introduced_by_diff": True,
        }
        finding.update(overrides)
        return finding

    def test_records_findings_with_trusted_context_hashes(self) -> None:
        result = review_finding_application.record_findings(
            self.subject,
            findings=(self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
            head_file_loader=lambda _path: None,
        )

        self.assertEqual(result.items[0]["context_hash"], "c" * 40)
        self.assertEqual(result.suggestions_recorded, 0)

    def test_missing_blob_hash_falls_back_to_exact_head(self) -> None:
        result = review_finding_application.record_findings(
            self.subject,
            findings=(self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 64,
                    context_hash_source="patch",
                ),
            ),
            head_file_loader=lambda _path: None,
        )

        self.assertEqual(result.items[0]["context_hash"], HEAD_SHA)

    def test_changed_path_matching_uses_the_persistence_normalization(self) -> None:
        result = review_finding_application.record_findings(
            self.subject,
            findings=(self.finding(path=" backend/changed.py "),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
            head_file_loader=lambda _path: None,
        )

        self.assertEqual(result.items[0]["path"], "backend/changed.py")

    def test_rejects_finding_outside_changed_files_before_persistence(self) -> None:
        with self.assertRaisesRegex(
            review_finding_application.ReviewFindingError,
            "every recorded finding must point to a changed pull-request file",
        ):
            review_finding_application.record_findings(
                self.subject,
                findings=(self.finding(path="backend/other.py"),),
                changed_files=(
                    review_finding_application.ChangedFile(
                        path="backend/changed.py",
                        context_hash="c" * 40,
                        context_hash_source="blob",
                    ),
                ),
                head_file_loader=lambda _path: None,
            )

        with closing(memory_db.connect_existing()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM finding_observations"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_optional_suggestion_is_validated_and_recorded(self) -> None:
        finding = self.finding(
            suggestion={
                "start_line": 1,
                "end_line": 1,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            }
        )
        reads: list[str] = []

        def load_head(path: str) -> str:
            reads.append(path)
            return "enabled = False"

        result = review_finding_application.record_findings(
            self.subject,
            findings=(finding,),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                    patch="@@ -0,0 +1 @@\n+enabled = False",
                ),
            ),
            head_file_loader=load_head,
        )

        self.assertEqual(result.suggestions_recorded, 1)
        self.assertEqual(result.items[0]["suggestion"], {"status": "recorded"})
        self.assertEqual(reads, ["backend/changed.py"])

    def test_rejected_candidates_cannot_exceed_the_head_file_read_limit(self) -> None:
        findings: list[dict[str, object]] = []
        changed_files: list[review_finding_application.ChangedFile] = []
        for index in range(memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW + 1):
            path = f"backend/candidate_{index:02d}.py"
            findings.append(
                self.finding(
                    rule_id=f"correctness.candidate-{index:02d}",
                    path=path,
                    symbol=f"candidate_{index}",
                    anchor=f"candidate {index}",
                    suggestion={
                        "start_line": 1,
                        "end_line": 1,
                        "expected_text": "expected = True",
                        "replacement_text": "expected = False",
                    },
                )
            )
            changed_files.append(
                review_finding_application.ChangedFile(
                    path=path,
                    context_hash="c" * 40,
                    context_hash_source="blob",
                    patch="@@ -0,0 +1 @@\n+actual = True",
                )
            )
        reads: list[str] = []

        def load_head(path: str) -> str:
            reads.append(path)
            return "actual = True"

        result = review_finding_application.record_findings(
            self.subject,
            findings=tuple(findings),
            changed_files=tuple(changed_files),
            head_file_loader=load_head,
        )

        self.assertEqual(result.suggestions_recorded, 0)
        self.assertEqual(
            len(reads), memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW
        )
        self.assertEqual(
            result.items[-1]["suggestion"],
            {"status": "omitted", "reason": "suggestion_review_limit"},
        )

    def test_context_lookup_returns_the_persisted_history(self) -> None:
        review_finding_application.record_findings(
            self.subject,
            findings=(self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
            head_file_loader=lambda _path: None,
        )

        context = review_finding_application.load_context(
            review_finding_application.FindingContextQuery(
                repository=REPOSITORY,
                paths=("backend/changed.py",),
                pr_number=1,
            )
        )

        self.assertEqual(context["repository"], REPOSITORY)
        self.assertEqual(context["paths"], ["backend/changed.py"])
        self.assertEqual(len(context["recent_findings"]), 1)


if __name__ == "__main__":
    unittest.main()
