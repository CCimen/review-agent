from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRIVY_REPORT_CHECK = ROOT / "scripts" / "check_trivy_report.py"


class TrivyPolicyTests(unittest.TestCase):
    def test_unfixed_high_is_reported_without_blocking(self):
        completed = self._run_policy(
            {
                "Results": [
                    {
                        "Target": "requirements.txt",
                        "Vulnerabilities": [
                            {
                                "Severity": "HIGH",
                                "FixedVersion": "",
                            }
                        ],
                    }
                ]
            },
            "--require-target",
            "requirements.txt",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "blocking_vulnerabilities": 0,
                "reports": 1,
                "required_targets": 1,
                "vulnerabilities": 1,
            },
            json.loads(completed.stdout),
        )

    def test_critical_and_fixable_high_are_blocking(self):
        for severity, fixed_version in (
            ("CRITICAL", ""),
            ("HIGH", "2.0.0"),
        ):
            with self.subTest(severity=severity, fixed_version=fixed_version):
                completed = self._run_policy(
                    {
                        "Results": [
                            {
                                "Target": "package-lock.json",
                                "Vulnerabilities": [
                                    {
                                        "Severity": severity,
                                        "FixedVersion": fixed_version,
                                    }
                                ],
                            }
                        ]
                    }
                )

                self.assertEqual(1, completed.returncode)
                self.assertEqual(
                    1,
                    json.loads(completed.stdout)["blocking_vulnerabilities"],
                )

    def test_missing_required_target_is_rejected(self):
        completed = self._run_policy(
            {"Results": [{"Target": "requirements.txt"}]},
            "--require-target",
            "website/package-lock.json",
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("missing required target", completed.stderr)

    def test_unknown_severity_is_rejected(self):
        completed = self._run_policy(
            {
                "Results": [
                    {
                        "Target": "requirements.txt",
                        "Vulnerabilities": [
                            {
                                "Severity": "SEVERE",
                                "FixedVersion": "9.9.9",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("unsupported vulnerability Severity", completed.stderr)

    def _run_policy(
        self,
        report: object,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(TRIVY_REPORT_CHECK),
                    *arguments,
                    str(report_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
