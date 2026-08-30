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
                "accepted_critical_occurrences": 0,
                "accepted_critical_vulnerabilities": 0,
                "blocking_vulnerabilities": 0,
                "reports": 1,
                "required_targets": 1,
                "vulnerabilities": 1,
            },
            json.loads(completed.stdout),
        )

    def test_fixable_high_and_critical_are_blocking(self):
        for severity, fixed_version in (
            ("CRITICAL", "2.0.0"),
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
                                        "VulnerabilityID": "CVE-2099-0001",
                                        "PkgName": "runtime",
                                        "InstalledVersion": "1.0.0",
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

    def test_unfixed_critical_blocks_without_an_exact_exception(self):
        completed = self._run_policy(
            {
                "Results": [
                    {
                        "Target": "container-image",
                        "Vulnerabilities": [
                            {
                                "Severity": "CRITICAL",
                                "FixedVersion": "",
                                "VulnerabilityID": "CVE-2099-0001",
                                "PkgName": "runtime",
                                "InstalledVersion": "1.0.0",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual(
            {
                "accepted_critical_occurrences": 0,
                "accepted_critical_vulnerabilities": 0,
                "blocking_vulnerabilities": 1,
                "reports": 1,
                "required_targets": 0,
                "vulnerabilities": 1,
            },
            json.loads(completed.stdout),
        )

    def test_exact_unexpired_release_exception_is_visible_and_bounded(self):
        exception = {
            "vulnerability_id": "CVE-2099-0001",
            "package_name": "runtime",
            "installed_version": "1.0.0",
            "reason": "No vendor fix; the vulnerable parser is not reachable.",
        }
        completed, summary = self._run_policy_with_exceptions(
            self._critical_report(),
            exceptions=[exception],
            expires_on="2999-12-31",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "accepted_critical_occurrences": 1,
                "accepted_critical_vulnerabilities": 1,
                "blocking_vulnerabilities": 0,
                "reports": 1,
                "required_targets": 0,
                "vulnerabilities": 1,
            },
            json.loads(completed.stdout),
        )
        self.assertIn("CVE-2099-0001", summary)
        self.assertIn("2999-12-31", summary)
        self.assertIn(exception["reason"], summary)

    def test_empty_release_exception_policy_accepts_a_clean_report(self):
        completed, summary = self._run_policy_with_exceptions(
            {
                "Results": [
                    {
                        "Target": "container-image",
                        "Vulnerabilities": [],
                    }
                ]
            },
            exceptions=[],
            expires_on="2999-12-31",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "accepted_critical_occurrences": 0,
                "accepted_critical_vulnerabilities": 0,
                "blocking_vulnerabilities": 0,
                "reports": 1,
                "required_targets": 0,
                "vulnerabilities": 0,
            },
            json.loads(completed.stdout),
        )
        self.assertIn("Image vulnerability review", summary)
        self.assertIn(
            "No critical package findings require a temporary exception.",
            summary,
        )

    def test_exception_does_not_match_a_changed_package_version(self):
        completed, summary = self._run_policy_with_exceptions(
            self._critical_report(installed_version="2.0.0"),
            exceptions=[
                {
                    "vulnerability_id": "CVE-2099-0001",
                    "package_name": "runtime",
                    "installed_version": "1.0.0",
                    "reason": "No vendor fix; the vulnerable parser is not reachable.",
                }
            ],
            expires_on="2999-12-31",
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("did not match", completed.stderr)
        self.assertEqual("", summary)

    def test_expired_release_exceptions_fail_closed(self):
        completed, summary = self._run_policy_with_exceptions(
            self._critical_report(),
            exceptions=[
                {
                    "vulnerability_id": "CVE-2099-0001",
                    "package_name": "runtime",
                    "installed_version": "1.0.0",
                    "reason": "No vendor fix; the vulnerable parser is not reachable.",
                }
            ],
            expires_on="2000-01-01",
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("expired", completed.stderr)
        self.assertEqual("", summary)

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

    def _run_policy_with_exceptions(
        self,
        report: object,
        *,
        exceptions: list[dict[str, str]],
        expires_on: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            report_path = temporary / "report.json"
            exceptions_path = temporary / "exceptions.json"
            summary_path = temporary / "summary.md"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            exceptions_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope": "release-image",
                        "expires_on": expires_on,
                        "exceptions": exceptions,
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRIVY_REPORT_CHECK),
                    "--critical-exceptions",
                    str(exceptions_path),
                    "--markdown-output",
                    str(summary_path),
                    str(report_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            summary = (
                summary_path.read_text(encoding="utf-8")
                if summary_path.exists()
                else ""
            )
            return completed, summary

    @staticmethod
    def _critical_report(*, installed_version: str = "1.0.0") -> object:
        return {
            "Results": [
                {
                    "Target": "container-image",
                    "Vulnerabilities": [
                        {
                            "Severity": "CRITICAL",
                            "FixedVersion": "",
                            "VulnerabilityID": "CVE-2099-0001",
                            "PkgName": "runtime",
                            "InstalledVersion": installed_version,
                        }
                    ],
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
