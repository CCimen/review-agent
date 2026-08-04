from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


def seed_false_positives(db_path: Path) -> None:
    finding = {
        "rule_id": "tenant.selector-scope",
        "category": "correctness",
        "path": "backend/src/intric/sysadmin/sysadmin_router.py",
        "line": 42,
        "symbol": "select_model",
        "anchor": "select_model",
        "title": "Tenant scope claim was wrong",
        "severity": "Medium",
        "publication_score": 7,
        "confidence": 0.9,
        "evidence": "The reviewer assumed the selector ignores tenant scope.",
        "disproof_checks": "Checked the selector, but not its caller arguments.",
        "impact": "The wrong model could be selected.",
        "smallest_fix": "Bind the tenant in the selector.",
        "introduced_by_diff": True,
    }
    with closing(memory_db.connect(str(db_path))) as connection:
        for offset, pr_number in enumerate((240, 241), start=1):
            head_sha = f"{offset}" * 40
            base_sha = f"{offset + 2}" * 40
            run = memory_db.start_run(
                connection,
                "eneo-ai/eneo",
                pr_number,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            recorded = memory_db.record_findings(
                connection,
                "eneo-ai/eneo",
                pr_number,
                head_sha,
                [finding],
                review_run_id=int(run["id"]),
                base_sha=base_sha,
                context_hashes={str(finding["path"]): "a" * 40},
            )[0]
            memory_db.add_decision(
                connection,
                str(recorded["fingerprint"]),
                "false_positive",
                f"Existing caller guard disproves this in PR {pr_number}.",
                "github:maintainer",
                observation_id=int(recorded["observation_id"]),
            )


class CoachRunCliTests(unittest.TestCase):
    def test_coach_run_writes_private_artifacts_and_records_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "coach-run"
            db_path = root / "memory.sqlite3"
            seed_false_positives(db_path)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "eneo_review_memory.py"),
                    "--db",
                    str(db_path),
                    "coach-run",
                    "--output-dir",
                    str(output_dir),
                    "--repo",
                    "eneo-ai/eneo",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["run"]["decision"], "propose")
            self.assertEqual(receipt["run"]["candidates_count"], 1)
            self.assertEqual(receipt["run"]["events_considered"], 2)

            for name in ["coach-export.json", "proposal.json", "SUMMARY.md"]:
                path = output_dir / name
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

            with closing(memory_db.connect(str(db_path))) as connection:
                runs = memory_db.list_coach_runs(connection, repository="eneo-ai/eneo")
                candidates = memory_db.list_coach_candidates(
                    connection, repository="eneo-ai/eneo"
                )

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].proposal_set_id, receipt["run"]["proposal_set_id"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].seen_count, 1)
            self.assertEqual(candidates[0].evidence_event_ids, ("decision:1", "decision:2"))

    def test_coach_run_reports_missing_database_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "eneo_review_memory.py"),
                    "--db",
                    str(root / "missing.sqlite3"),
                    "coach-run",
                    "--output-dir",
                    str(root / "coach-run"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("review memory database does not exist", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
