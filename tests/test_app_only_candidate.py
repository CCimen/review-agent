from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _candidate_gate():
    spec = importlib.util.spec_from_file_location(
        "review_agent_candidate_gate_test",
        ROOT / "scripts/check_app_only_candidate.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load App-only candidate gate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppOnlyCandidateTests(unittest.TestCase):
    def test_residual_scanner_reports_path_line_and_signature(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                "GITHUB_READ_TOKEN: still-live\n", encoding="utf-8"
            )

            findings = gate.scan_residual_paths(root, (Path("compose.yaml"),))

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "compose.yaml")
        self.assertEqual(findings[0].line, 1)
        self.assertEqual(findings[0].rule, "legacy GitHub credential")

    def test_repository_satisfies_app_only_candidate_contract(self) -> None:
        gate = _candidate_gate()
        self.assertEqual(gate.check_candidate(ROOT), [])

    def test_topology_rejects_list_form_key_outside_gateway(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                """services:
  hermes-review:
    environment:
      - REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE=/tmp/key
  review-github-gateway:
    secrets:
      - github_app_private_key
""",
                encoding="utf-8",
            )

            rules = {finding.rule for finding in gate.check_candidate(root)}

        self.assertIn("GitHub credential exposed to hermes-review", rules)
        self.assertTrue(
            any(rule.startswith("App private key holders must") for rule in rules)
        )

    def test_topology_rejects_a_public_gateway(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                """services:
  review-github-gateway:
    environment:
      REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE: /run/secrets/key
    ports:
      - 8646:8646
""",
                encoding="utf-8",
            )

            rules = {finding.rule for finding in gate.check_candidate(root)}

        self.assertIn("GitHub gateway is public", rules)

    def test_topology_rejects_a_gateway_router(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                """services:
  review-github-gateway:
    environment:
      REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE: /run/secrets/key
    labels:
      - traefik.http.routers.review-gateway.rule=Host(`review.example.org`)
""",
                encoding="utf-8",
            )

            rules = {finding.rule for finding in gate.check_candidate(root)}

        self.assertIn("GitHub gateway is public", rules)

    def test_topology_rejects_a_token_on_admission(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                """services:
  review-admission:
    environment:
      REVIEW_AGENT_GITHUB_TOKEN: forbidden
  review-github-gateway:
    environment:
      REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE: /run/secrets/key
""",
                encoding="utf-8",
            )

            rules = {finding.rule for finding in gate.check_candidate(root)}

        self.assertIn("GitHub credential exposed to review-admission", rules)

    def test_missing_compose_is_a_readable_finding(self) -> None:
        gate = _candidate_gate()
        with tempfile.TemporaryDirectory() as directory:
            findings = gate.check_candidate(Path(directory))

        self.assertIn("compose.yaml is missing", {item.rule for item in findings})


if __name__ == "__main__":
    unittest.main()
