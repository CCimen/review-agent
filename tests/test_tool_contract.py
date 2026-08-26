from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

import review_agent_tools  # noqa: E402
from review_agent_tools import review_contract, review_run_application, schemas, tools  # noqa: E402
from review_agent_tools.domain.review import resolve_review_subject  # noqa: E402
from review_agent_tools.github.source import ReviewFilePage  # noqa: E402
from review_agent_tools.postgres.coverage import (  # noqa: E402
    FileIndexSummary,
    RunFile,
    RunFilePage,
)
from review_agent_tools.domain.review import ReviewRunId  # noqa: E402

TEST_REVIEW_CONTRACT = review_contract.ReviewContract(
    profile="sundsvall-standard",
    hermes_image="hermes@test",
    model_provider="openai-codex",
    model="gpt-test",
    reasoning_effort="high",
    plugin_result_max_chars=160_000,
    profile_bundle_sha256="1" * 64,
    managed_config_sha256="2" * 64,
    engine_bundle_sha256="3" * 64,
    sha256="contract",
)


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
    session_id = "review-agent-job-7-lease-3"

    @staticmethod
    def _runtime() -> Mock:
        runtime = Mock()
        runtime.transaction.return_value = nullcontext(Mock())
        return runtime

    @staticmethod
    def _live_state() -> review_run_application.LiveRunState:
        return review_run_application.LiveRunState(
            run_id=41,
            phase="reviewing",
            started_at="2026-08-24T10:00:00Z",
            file_index=FileIndexSummary(
                changed_files_reported=1,
                changed_files_registered=1,
                registration_complete=True,
                by_domain=(("general", 1),),
                by_review_mode=(("normal", 1),),
                by_change_status=(("modified", 1),),
                sample_paths=(),
            ),
            resolved_config=resolve_review_subject(
                base_sha="a" * 40,
                head_sha="b" * 40,
                policy_revision="policy-v1",
                resolved_config_schema_version=2,
                resolved_config=review_contract.resolved_config(TEST_REVIEW_CONTRACT),
            ).resolved_config,
        )

    def test_source_schemas_accept_only_run_scoped_authority(self) -> None:
        source_schemas = (
            schemas.REVIEW_AGENT_BEGIN,
            schemas.REVIEW_AGENT_PR_FILES,
            schemas.REVIEW_AGENT_PR_DIFF,
            schemas.REVIEW_AGENT_PR_FILE,
            schemas.REVIEW_AGENT_MEMORY_CONTEXT,
            schemas.REVIEW_AGENT_MEMORY_RECORD,
            schemas.REVIEW_AGENT_DELIVER,
        )
        for schema in source_schemas:
            parameters = schema["parameters"]
            self.assertFalse(parameters["additionalProperties"])
            properties = parameters["properties"]
            self.assertNotIn("repository", properties)
            self.assertNotIn("pr_number", properties)
            self.assertNotIn("head_sha", properties)

        self.assertEqual(
            schemas.REVIEW_AGENT_BEGIN["parameters"]["required"],
            ["existing_run_id"],
        )

    def test_delivery_schema_describes_the_durable_publisher_handoff(self) -> None:
        description = schemas.REVIEW_AGENT_DELIVER["description"]
        self.assertIn("queue immutable publication intent", description)
        self.assertIn("separate recoverable publisher", description)

    def test_worker_continues_the_exact_run_without_starting_another(self) -> None:
        pull = {
            "state": "open",
            "title": "Continue",
            "base": {
                "sha": "a" * 40,
                "ref": "main",
                "repo": {"id": 1, "full_name": self.repository},
            },
            "head": {
                "sha": "b" * 40,
                "ref": "change",
                "repo": {"id": 1, "full_name": self.repository},
            },
            "changed_files": 1,
        }
        source = SimpleNamespace(run_id=41)
        runtime = self._runtime()
        with (
            patch.object(tools, "_postgres_runtime", return_value=runtime),
            patch.object(tools.postgres_jobs, "require_live_lease"),
            patch.object(tools, "_gateway_source_session", return_value=source),
            patch.object(tools, "_pr", return_value=(self.repository, 1, pull)),
            patch.object(
                tools.review_run_application,
                "load_live_run_state",
                return_value=self._live_state(),
            ),
            patch.object(
                tools,
                "_review_run_snapshot",
                return_value=(pull, {"base_sha": "a" * 40, "head_sha": "b" * 40}),
            ),
            patch.object(tools.review_run_application, "start_live_review") as start,
            patch.object(tools, "_changed_files") as changed,
            patch.object(
                tools.review_contract,
                "load_installed_contract",
                return_value=TEST_REVIEW_CONTRACT,
            ),
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_PROFILE": "sundsvall-standard"},
                clear=True,
            ),
        ):
            result = json.loads(
                tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        self.assertEqual(result["run_id"], 41)
        self.assertEqual(result["phase"], "reviewing")
        self.assertTrue(result["continued"])
        start.assert_not_called()
        changed.assert_not_called()

    def test_pr_files_returns_the_persisted_run_inventory(self) -> None:
        pull = {
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        }
        page = RunFilePage(
            run_id=ReviewRunId(41),
            repository=self.repository,
            pr_number=1,
            limit=100,
            next_cursor=None,
            total_matching=1,
            items=(
                RunFile(
                    path="src/app.py",
                    change_status="modified",
                    previous_path="",
                    domain="backend",
                    review_mode="normal",
                    diff_state=review_run_application.DiffState.UNSEEN,
                    is_changed_path=True,
                ),
            ),
        )
        source = SimpleNamespace(run_id=41)
        with (
            patch.object(tools, "_gateway_source_session", return_value=source),
            patch.object(tools, "_pr", return_value=(self.repository, 1, pull)),
            patch.object(tools, "_postgres_runtime", return_value=self._runtime()),
            patch.object(
                tools.review_run_application,
                "load_live_changed_file_page",
                return_value=page,
            ),
        ):
            result = json.loads(tools.pr_files.__wrapped__({"run_id": 41}))

        self.assertEqual(result["total_matching"], 1)
        self.assertEqual(result["items"][0]["path"], "src/app.py")

    def test_pr_diff_falls_back_when_the_whole_diff_is_unavailable(self) -> None:
        pull = {
            "changed_files": 1,
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        }
        for state, truncated in (("diff_unavailable", False), ("ok", True)):
            with self.subTest(state=state, truncated=truncated):
                client = Mock()
                client.get_review_diff.return_value = SimpleNamespace(
                    state=state,
                    body=b"partial diff",
                    truncated=truncated,
                )
                source = SimpleNamespace(
                    run_id=41,
                    lease=SimpleNamespace(job_id=7, lease_generation=3),
                    client=client,
                )
                with (
                    patch.object(
                        tools, "_gateway_source_session", return_value=source
                    ),
                    patch.object(
                        tools, "_pr", return_value=(self.repository, 1, pull)
                    ),
                    patch.object(
                        tools, "_review_run_snapshot", return_value=(pull, {})
                    ),
                    patch.object(
                        tools,
                        "_pr_diff_from_patches",
                        return_value=json.dumps({"diff_source": "per_file_patch"}),
                    ) as fallback,
                ):
                    result = json.loads(tools.pr_diff.__wrapped__({"run_id": 41}))

                self.assertEqual(result["diff_source"], "per_file_patch")
                fallback.assert_called_once()

    def test_pr_file_reads_a_renamed_base_file_at_its_previous_path(self) -> None:
        pull = {
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        }
        client = Mock()
        client.get_review_file_page.return_value = ReviewFilePage(
            state="ok",
            repository=self.repository,
            revision="a" * 40,
            start_line=1,
            total_lines=1,
            content="1: previous content",
            complete_lines=1,
            partial_line=False,
        )
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
            client=client,
        )
        snapshot = SimpleNamespace(
            pull=pull,
            run=SimpleNamespace(base_sha="a" * 40, head_sha="b" * 40),
        )
        run_file = SimpleNamespace(
            item=SimpleNamespace(
                is_changed_path=True,
                change_status="renamed",
                previous_path="src/old.py",
            ),
            registration_complete=True,
        )
        with (
            patch.object(tools, "_gateway_source_session", return_value=source),
            patch.object(tools, "_pr", return_value=(self.repository, 1, pull)),
            patch.object(tools, "_postgres_runtime", return_value=self._runtime()),
            patch.object(
                tools.review_run_application,
                "load_live_file_context",
                return_value=(snapshot, run_file),
            ),
            patch.object(
                tools.review_run_application, "record_live_source_read"
            ) as record,
        ):
            result = json.loads(
                tools.pr_file.__wrapped__(
                    {"run_id": 41, "path": "src/new.py", "side": "base"}
                )
            )

        self.assertEqual(result["content"], "1: previous content")
        self.assertEqual(client.get_review_file_page.call_args.kwargs["path"], "src/old.py")
        record.assert_called_once()

    def test_pr_file_rejects_a_gateway_page_for_another_revision(self) -> None:
        pull = {
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        }
        client = Mock()
        client.get_review_file_page.return_value = ReviewFilePage(
            state="ok",
            repository=self.repository,
            revision="c" * 40,
            start_line=1,
            total_lines=1,
            content="1: wrong revision",
            complete_lines=1,
            partial_line=False,
        )
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
            client=client,
        )
        snapshot = SimpleNamespace(
            pull=pull,
            run=SimpleNamespace(base_sha="a" * 40, head_sha="b" * 40),
        )
        run_file = SimpleNamespace(item=None, registration_complete=True)
        with (
            patch.object(tools, "_gateway_source_session", return_value=source),
            patch.object(tools, "_pr", return_value=(self.repository, 1, pull)),
            patch.object(tools, "_postgres_runtime", return_value=self._runtime()),
            patch.object(
                tools.review_run_application,
                "load_live_file_context",
                return_value=(snapshot, run_file),
            ),
        ):
            result = json.loads(
                tools.pr_file.__wrapped__(
                    {"run_id": 41, "path": "src/app.py", "side": "head"}
                )
            )

        self.assertEqual(
            result["error"], "GitHub gateway returned a different review subject"
        )

    def test_missing_worker_session_fails_before_source_or_database_work(self) -> None:
        with (
            patch.object(tools, "_gateway_source_session") as source,
            patch.object(tools, "_postgres_runtime") as runtime,
        ):
            result = json.loads(tools.review_begin({"existing_run_id": 41}))

        self.assertEqual(
            result["error"],
            "a live review worker lease is required; stop this review turn",
        )
        source.assert_not_called()
        runtime.assert_not_called()

    def test_every_model_handler_requires_a_worker_lease(self) -> None:
        handlers = (
            (tools.review_begin, {"existing_run_id": 41}),
            (tools.pr_files, {"run_id": 41}),
            (tools.pr_diff, {"run_id": 41}),
            (tools.pr_file, {"run_id": 41}),
            (tools.review_memory_context, {"run_id": 41}),
            (tools.review_memory_record, {"run_id": 41}),
            (tools.review_deliver, {"run_id": 41}),
        )
        for handler, args in handlers:
            with self.subTest(handler=handler.__name__):
                result = json.loads(handler(args))
                self.assertEqual(
                    result["error"],
                    "a live review worker lease is required; stop this review turn",
                )

    def test_malformed_worker_session_fails_before_source_or_database_work(self) -> None:
        with (
            patch.object(tools, "_gateway_source_session") as source,
            patch.object(tools, "_postgres_runtime") as runtime,
        ):
            result = json.loads(
                tools.review_begin(
                    {"existing_run_id": 41},
                    session_id="review-agent-job-invalid",
                )
            )

        self.assertIn("worker session identity is malformed", result["error"])
        source.assert_not_called()
        runtime.assert_not_called()

    def test_worker_fence_fails_closed_on_unexpected_runtime_error(self) -> None:
        runtime = Mock()
        runtime.transaction.side_effect = RuntimeError("internal detail")
        with (
            patch.object(tools, "_postgres_runtime", return_value=runtime),
            patch.object(tools, "_gateway_source_session") as source,
            self.assertLogs("review_agent_tools.tools", level="ERROR"),
        ):
            result = json.loads(
                tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        self.assertEqual(
            result["error"],
            "worker lease could not be verified; stop this review turn",
        )
        source.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
