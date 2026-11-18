from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import unittest

import yaml

from scripts.validate_release_tag import is_release_tag


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_agent.cli import admin as admin_cli  # noqa: E402


def _llms_generator_module():
    scripts_path = str(ROOT / "scripts")
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "review_agent_llms_generator_test",
            ROOT / "scripts/generate_llms_docs.py",
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load LLM documentation generator")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts_path)


class AiOnboardingContractTests(unittest.TestCase):
    def test_generated_llm_context_and_skill_mirrors_are_current(self) -> None:
        for script in (
            "scripts/generate_llms_docs.py",
            "scripts/sync_install_skill.py",
        ):
            with self.subTest(script=script):
                subprocess.run(
                    [sys.executable, str(ROOT / script), "--check"],
                    check=True,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )

        short = (ROOT / "website/static/llms.txt").read_text(encoding="utf-8")
        release_lines = [
            line for line in short.splitlines() if line.startswith("Release state: ")
        ]
        self.assertEqual(len(release_lines), 1)
        release_tag = release_lines[0].removeprefix("Release state: ")
        self.assertTrue(is_release_tag(release_tag))

        generator = _llms_generator_module()
        repository_context = (ROOT / "docs/REPOSITORY_CONTEXT.md").read_text(
            encoding="utf-8"
        )
        public_image_tags = set(
            re.findall(
                r"ghcr\.io/ccimen/review-agent:(v[0-9A-Za-z.-]+)",
                repository_context,
            )
        )
        self.assertEqual(public_image_tags, {generator.REVISION})
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            f"v{project['project']['version']}",
            generator.REVISION,
            "bump version in pyproject.toml with REVISION in scripts/generate_llms_docs.py",
        )
        for invalid_tag in (
            "v01.2.3",
            "v1.2.3-01",
            "v1.2.3-.rc",
            "v1.2.3-rc..1",
        ):
            with self.subTest(invalid_tag=invalid_tag):
                with self.assertRaises(generator.GenerationError):
                    generator.generate(revision=invalid_tag)

    def test_installation_plan_is_non_secret_and_has_no_scale_ceiling(self) -> None:
        plan = yaml.safe_load(
            (ROOT / "install/review-agent.example.yaml").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "install/review-agent.schema.json").read_text(encoding="utf-8")
        )
        rendered = json.dumps(plan, sort_keys=True)
        for forbidden in (
            "api_key",
            "database_url",
            "password",
            "private_key",
            "secret",
            "token",
        ):
            self.assertNotIn(forbidden, rendered.casefold())

        runtime_properties = schema["properties"]["runtime"]["properties"]
        self.assertTrue(
            all("maximum" not in definition for definition in runtime_properties.values())
        )
        self.assertIs(plan["deployment"]["advisory_only"], True)
        self.assertEqual(plan["github"]["installation_mode"], "organization_managed")
        self.assertEqual(plan["github"]["repositories"], [])

    def test_admin_parser_accepts_the_operator_commands(self) -> None:
        parser = admin_cli._parser()
        commands = (
            ("capabilities",),
            ("preflight",),
            ("doctor",),
            ("queues", "inspect"),
            ("installations", "list"),
            ("repositories", "list"),
            (
                "smoke-test",
                "--dry-run",
                "--repository",
                "owner/repository",
                "--pr",
                "1",
            ),
            (
                "github-app",
                "registration-url",
                "--owner",
                "owner",
                "--owner-type",
                "user",
                "--public-url",
                "https://review.example.org",
                "--homepage-url",
                "https://docs.example.org/review-agent/",
            ),
            (
                "github-app",
                "onboard",
                "owner/repository",
                "--actor",
                "github:operator",
            ),
            (
                "github-app",
                "approve",
                "1",
                "--actor",
                "github:operator",
                "--reason",
                "approved organization installation",
            ),
        )
        for command in commands:
            with self.subTest(command=command):
                parser.parse_args(command)

    def test_repository_only_starter_validates_with_the_documented_cli(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "review_agent.cli.admin",
                "repository-context",
                "validate",
                str(ROOT / "examples/repository-context"),
            ],
            check=False,
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertIs(receipt["ready"], True)
        self.assertIs(receipt["configured"], True)


if __name__ == "__main__":
    unittest.main()
