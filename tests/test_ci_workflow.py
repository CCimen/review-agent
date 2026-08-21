from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
POSTGRES_CHECK = ROOT / "scripts" / "check_postgres_schema.sh"
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
                "python3 -m pip install --disable-pip-version-check --requirement requirements.txt",
                "npm install --global pyright@1.1.408",
                "./scripts/check_postgres_schema.sh",
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

    def test_postgresql_contract_uses_one_pinned_loopback_only_database(self):
        source = POSTGRES_CHECK.read_text(encoding="utf-8")
        image = (
            "postgres:17.10-bookworm@"
            "sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f"
        )

        self.assertEqual(source.count(image), 1)
        self.assertIn('cd "$ROOT"', source)
        self.assertIn("docker run", source)
        self.assertIn("--rm", source)
        self.assertIn("docker rm --force", source)
        self.assertIn("trap ", source)
        self.assertIn('REVIEW_AGENT_POSTGRES_CONTAINER="$CONTAINER"', source)
        for test_module in (
            "tests.test_postgres_schema",
            "tests.test_postgres_migrations",
            "tests.test_postgres_runtime",
        ):
            self.assertIn(test_module, source)
        self.assertIn("--publish 127.0.0.1::5432", source)
        self.assertNotIn("0.0.0.0", source)
        self.assertNotRegex(source, r"127\.0\.0\.1:[0-9]+:5432")


class MigrationReadinessDocumentationTests(unittest.TestCase):
    def test_public_status_names_the_clean_postgresql_replacement(self):
        roadmap = ROADMAP.read_text(encoding="utf-8")
        homepage = HOMEPAGE.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        normalized_roadmap = re.sub(r"\s+", " ", roadmap)
        normalized_homepage = re.sub(r"\s+", " ", homepage)

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
            "initial PostgreSQL schema",
            "real PostgreSQL",
            "active reviewer still uses SQLite",
            "checksum-verifying migration runner",
            "read-only PostgreSQL runtime foundation",
            "provider repository ID acquisition",
            "runtime configuration and Compose",
            "Delete the SQLite application persistence",
            "Hermes `HERMES_HOME` state is separate",
            "clean runtime replacement",
        ):
            self.assertIn(migration_invariant, normalized_roadmap)

        self.assertIn("durable jobs and an outbox", normalized_roadmap)
        recovery = "Define the initial PostgreSQL replacement recovery path"
        cutover = "Switch runtime configuration and Compose to PostgreSQL"
        self.assertIn(recovery, normalized_roadmap)
        self.assertIn("previous PostgreSQL-compatible application image", normalized_roadmap)
        self.assertLess(normalized_roadmap.index(recovery), normalized_roadmap.index(cutover))
        self.assertNotIn("PostgreSQL is deployed", normalized_roadmap)
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
        self.assertIn("one PostgreSQL database per environment", normalized_homepage)
        self.assertIn("initial PostgreSQL schema", normalized_homepage)
        self.assertIn("real-engine CI contract", normalized_homepage)
        self.assertIn("still uses SQLite", normalized_homepage)
        self.assertIn("GitHub Actions runs the same bundle", readme)


if __name__ == "__main__":
    unittest.main()
