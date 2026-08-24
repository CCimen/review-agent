from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

import review_agent_tools  # noqa: E402
from review_agent_tools import tools  # noqa: E402


class _FakeRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}

    def get_config(self, key: str, default: object = None) -> object:
        del key
        return default

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
    ) -> None:
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }


class ToolContractTests(unittest.TestCase):
    repository = "example-org/example-repository"

    def test_empty_allowlist_denies_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {"REVIEW_AGENT_ALLOWED_REPOSITORIES": ""},
            clear=True,
        ):
            result = json.loads(
                tools.review_begin(
                    {"repository": self.repository, "pr_number": 1}
                )
            )

        self.assertIn("deny by default", result["error"])

    def test_allowlist_is_accepted_before_other_input_validation(self) -> None:
        with patch.dict(
            os.environ,
            {"REVIEW_AGENT_ALLOWED_REPOSITORIES": self.repository},
            clear=True,
        ):
            result = json.loads(
                tools.review_begin(
                    {"repository": self.repository, "pr_number": 0}
                )
            )

        self.assertEqual(result["error"], "pr_number must be positive")

    def test_non_allowlisted_repository_is_denied_before_network(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_ALLOWED_REPOSITORIES": self.repository},
                clear=True,
            ),
            patch.object(tools, "_pr") as pull_reader,
            patch.object(tools, "_request") as requester,
        ):
            result = json.loads(
                tools.pr_diff(
                    {
                        "repository": "other-org/other-repository",
                        "pr_number": 1,
                        "run_id": 1,
                    }
                )
            )

        self.assertEqual(result["error"], "repository is not allowlisted")
        pull_reader.assert_not_called()
        requester.assert_not_called()

    def test_plugin_manifest_and_registered_handlers_have_one_owner(self) -> None:
        registry = _FakeRegistry()

        review_agent_tools.register(registry)

        manifest = (PACKAGE_ROOT / "review_agent_tools" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
        declared_block = manifest.partition("provides_tools:")[2].partition(
            "requires_env:"
        )[0]
        declared = {
            line.strip().removeprefix("- ").strip()
            for line in declared_block.splitlines()
            if line.strip().startswith("- ")
        }
        self.assertEqual(set(registry.tools), declared)
        for name, registration in registry.tools.items():
            self.assertEqual(registration["toolset"], "review_agent")
            schema = registration["schema"]
            self.assertIsInstance(schema, dict)
            assert isinstance(schema, dict)
            self.assertEqual(schema["name"], name)
            self.assertTrue(callable(registration["handler"]))

    def test_terminal_snapshot_handoff_is_reused_without_github_reads(self) -> None:
        terminal_payload = {
            "run_id": 41,
            "run_state": "snapshot_superseded",
            "terminal": True,
            "retryable": False,
        }
        load = Mock(
            side_effect=(
                tools.ReviewRunTerminal(41, newly_terminalized=True),
                tools.ReviewRunTerminal(41, newly_terminalized=False),
            )
        )
        with (
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_ALLOWED_REPOSITORIES": self.repository},
                clear=True,
            ),
            patch.object(
                tools.review_run_application,
                "load_live_snapshot",
                load,
            ),
            patch.object(tools, "_postgres_runtime"),
            patch.object(tools, "_pr") as pull_reader,
            patch.object(
                tools, "_run_terminal_payload", return_value=terminal_payload
            ),
            patch.object(tools, "_publish_failure_status_safe") as publish_status,
        ):
            arguments = {
                "repository": self.repository,
                "pr_number": 1,
                "head_sha": "a" * 40,
                "run_id": 41,
            }
            first = json.loads(tools.review_deliver(arguments))
            second = json.loads(tools.review_deliver(arguments))

        self.assertEqual(first, terminal_payload)
        self.assertEqual(second, terminal_payload)
        pull_reader.assert_not_called()
        publish_status.assert_called_once_with(
            run_id=41,
            failure_code="snapshot_superseded",
        )


if __name__ == "__main__":
    unittest.main()
