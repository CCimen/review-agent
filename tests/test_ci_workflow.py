from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
HOMEPAGE = ROOT / "website" / "src" / "pages" / "index.tsx"
README = ROOT / "README.md"


class PythonBundleWorkflowTests(unittest.TestCase):
    def test_full_bundle_runs_through_one_read_only_pinned_workflow(self):
        self.assertTrue(WORKFLOW.is_file(), "full Python bundle CI is missing")
        source = WORKFLOW.read_text(encoding="utf-8")

        expected_header = """name: Python bundle

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: python-bundle-${{ github.ref }}
  cancel-in-progress: true
"""
        self.assertTrue(source.startswith(expected_header))
        self.assertNotIn("pull_request_target", source)
        self.assertEqual(source.count("permissions:"), 1)
        self.assertNotRegex(source, r"(?m)^\s+[^:#]+:\s*write\b")

        expected_actions = [
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
            "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444",
        ]
        actions = re.findall(r"(?m)^\s+uses: ([^\s]+)$", source)
        self.assertEqual(actions, expected_actions)
        for action in actions:
            self.assertRegex(action, r"@[0-9a-f]{40}$")

        self.assertIn("python-version: '3.11'", source)
        checkout_stanza = re.compile(
            r"(?m)^      - name: Check out repository\n"
            r"        uses: actions/checkout@"
            r"d23441a48e516b6c34aea4fa41551a30e30af803\n"
            r"        with:\n"
            r"          persist-credentials: false$"
        )
        self.assertEqual(len(checkout_stanza.findall(source)), 1)
        self.assertEqual(source.count("persist-credentials: false"), 1)
        self.assertIn("npm install --global pyright@1.1.408", source)
        run_commands = re.findall(r"(?m)^\s+run: (.+)$", source)
        self.assertEqual(
            run_commands,
            [
                "npm install --global pyright@1.1.408",
                "./scripts/check_bundle.sh",
            ],
        )
        for duplicated_command in (
            "python3 -m compileall",
            "python3 -m unittest",
            "pyright -p",
            "validate-replay",
        ):
            self.assertNotIn(duplicated_command, source)


class MigrationReadinessDocumentationTests(unittest.TestCase):
    def test_public_status_names_the_clean_postgresql_replacement(self):
        roadmap = ROADMAP.read_text(encoding="utf-8")
        homepage = HOMEPAGE.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        normalized_roadmap = re.sub(r"\s+", " ", roadmap)

        for current_capability in (
            "Full Python bundle CI",
            "typed runtime settings",
            "bounded GitHub read client",
            "run and finding application owners",
        ):
            self.assertIn(current_capability, normalized_roadmap)

        for migration_invariant in (
            "one PostgreSQL database per environment",
            "No per-repository databases",
            "no permanent dual writes",
            "temporary and disposable",
            "PostgreSQL schema and transaction boundary",
            "clean runtime replacement",
            "previous PostgreSQL-compatible application image against the same PostgreSQL database",
        ):
            self.assertIn(migration_invariant, normalized_roadmap)

        self.assertIn("Jobs, leases, and an outbox follow", normalized_roadmap)
        self.assertNotIn("PostgreSQL is deployed", normalized_roadmap)
        recovery = normalized_roadmap.index(
            "Define and test initial replacement recovery before switching"
        )
        switch = normalized_roadmap.index(
            "Switch runtime configuration and Compose to PostgreSQL"
        )
        self.assertLess(recovery, switch)
        for retired_direction in (
            "SQLite importer",
            "SQLite rollback",
            "stable SQLite identities",
            "forever dual-backend abstraction",
        ):
            self.assertNotIn(retired_direction, normalized_roadmap)
        self.assertNotIn(
            "Typed ownership and trusted project context come before PostgreSQL",
            homepage,
        )
        self.assertIn("one PostgreSQL database per environment", homepage)
        self.assertIn("GitHub Actions runs the same bundle", readme)


if __name__ == "__main__":
    unittest.main()
