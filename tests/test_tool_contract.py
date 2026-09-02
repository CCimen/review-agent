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
from review_agent_tools import (  # noqa: E402
    capacity,
    review_contract,
    review_delivery_tool,
    review_memory_tools,
    repository_decision_context,
    repository_guidance_context,
    review_run_application,
    review_source_tools,
    review_tool_runtime,
    schemas,
)
from review_agent_tools.domain.repository_decisions import RepositoryDecision  # noqa: E402
from review_agent_tools.domain.review import (  # noqa: E402
    CoverageState,
    resolve_review_subject,
)
from review_agent_tools.github.source import ReviewFilePage  # noqa: E402
from review_agent_tools.postgres.coverage import (  # noqa: E402
    CoverageSummary,
    FileIndexSummary,
    RunFile,
    RunFileLookup,
    RunFilePage,
)
from review_agent_tools.domain.review import ReviewRunId  # noqa: E402

TEST_REVIEW_CONTRACT = review_contract.ReviewContract(
    profile="default-standard",
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
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        runtime = self._runtime()
        with (
            patch.object(review_tool_runtime, "postgres_runtime", return_value=runtime),
            patch.object(review_source_tools, "postgres_runtime", return_value=runtime),
            patch.object(review_tool_runtime.postgres_jobs, "require_live_lease"),
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=self._live_state(),
            ),
            patch.object(
                review_source_tools,
                "review_run_snapshot",
                return_value=pull,
            ),
            patch.object(
                review_run_application,
                "load_or_create_live_repository_guidance",
                return_value=repository_guidance_context.not_configured(
                    base_sha="a" * 40,
                ),
            ),
            patch.object(
                review_run_application,
                "load_or_create_live_repository_decisions",
                return_value=repository_decision_context.not_configured(
                    base_sha="a" * 40,
                ),
            ),
            patch.object(review_run_application, "start_live_review") as start,
            patch.object(review_source_tools, "load_changed_files") as changed,
            patch.object(
                review_contract,
                "load_installed_contract",
                return_value=TEST_REVIEW_CONTRACT,
            ),
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_PROFILE": "default-standard"},
                clear=True,
            ),
        ):
            result = json.loads(
                review_source_tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        self.assertEqual(result["run_id"], 41)
        self.assertEqual(result["phase"], "reviewing")
        self.assertTrue(result["continued"])
        self.assertEqual(
            result["repository_guidance_untrusted"]["status"],
            "not_configured",
        )
        self.assertEqual(
            result["repository_decisions_untrusted"]["status"],
            "not_configured",
        )
        self.assertEqual(
            result["repository_decisions_untrusted"]["decisions"],
            [],
        )
        start.assert_not_called()
        changed.assert_not_called()

    def test_review_begin_loads_ordered_guidance_once_and_reuses_its_snapshot(self) -> None:
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

        def page(content: str) -> ReviewFilePage:
            lines = content.splitlines()
            return ReviewFilePage(
                state="ok",
                repository=self.repository,
                revision="a" * 40,
                start_line=1,
                total_lines=len(lines),
                content="\n".join(
                    f"{number}: {line}"
                    for number, line in enumerate(lines, start=1)
                ),
                complete_lines=len(lines),
                partial_line=False,
            )

        client = Mock()
        client.get_review_file_page.side_effect = [
            page(
                'version = 1\ncontext = ["context/platform.md", '
                '"context/service.md"]'
            ),
            page("Prefer one clear owner for each lifecycle transition."),
            page("The platform owns authentication."),
            page("This service owns billing reconciliation."),
        ]
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
            client=client,
        )
        runtime = self._runtime()
        stored: list[repository_guidance_context.RepositoryGuidanceContext] = []

        def load_context(*_args: object, **_kwargs: object):
            if stored:
                return stored[0]
            return repository_guidance_context.pending(base_sha="a" * 40)

        def store_context(
            *_args: object,
            context: repository_guidance_context.RepositoryGuidanceContext,
            **_kwargs: object,
        ) -> repository_guidance_context.RepositoryGuidanceContext:
            stored.append(context)
            return context

        with (
            patch.object(review_tool_runtime, "postgres_runtime", return_value=runtime),
            patch.object(review_source_tools, "postgres_runtime", return_value=runtime),
            patch.object(review_tool_runtime.postgres_jobs, "require_live_lease"),
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=self._live_state(),
            ),
            patch.object(
                review_source_tools,
                "review_run_snapshot",
                return_value=pull,
            ),
            patch.object(review_run_application, "_require_live_scope"),
            patch.object(
                review_run_application.postgres_repository_guidance,
                "load_context",
                side_effect=load_context,
            ),
            patch.object(
                review_run_application.postgres_repository_guidance,
                "store_context",
                side_effect=store_context,
            ),
            patch.object(
                review_run_application,
                "load_or_create_live_repository_decisions",
                return_value=repository_decision_context.not_configured(
                    base_sha="a" * 40,
                ),
            ),
            patch.object(
                review_contract,
                "load_installed_contract",
                return_value=TEST_REVIEW_CONTRACT,
            ),
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_PROFILE": "default-standard"},
                clear=True,
            ),
        ):
            first = json.loads(
                review_source_tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )
            second = json.loads(
                review_source_tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        first_guidance = first["repository_guidance_untrusted"]
        second_guidance = second["repository_guidance_untrusted"]
        self.assertEqual(first_guidance["status"], "loaded")
        self.assertEqual(first_guidance["base_sha"], "a" * 40)
        self.assertEqual(
            first_guidance["instructions"]["content"],
            "Prefer one clear owner for each lifecycle transition.",
        )
        self.assertEqual(
            [item["path"] for item in first_guidance["context_files"]],
            [
                ".review-agent/context/platform.md",
                ".review-agent/context/service.md",
            ],
        )
        self.assertEqual(
            [item["content"] for item in first_guidance["context_files"]],
            [
                "The platform owns authentication.",
                "This service owns billing reconciliation.",
            ],
        )
        self.assertEqual(
            [
                call.kwargs["path"]
                for call in client.get_review_file_page.call_args_list
            ],
            [
                ".review-agent/config.toml",
                ".review-agent/instructions.md",
                ".review-agent/context/platform.md",
                ".review-agent/context/service.md",
            ],
        )
        self.assertEqual(first_guidance, second_guidance)
        self.assertEqual(client.get_review_file_page.call_count, 4)
        self.assertEqual(len(stored), 1)

    def test_stored_adr_payload_degrades_when_the_response_budget_is_reduced(self) -> None:
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
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        decisions = tuple(
            RepositoryDecision(
                id=f"ADR-{number:04d}",
                adr_path=f".review-agent/decisions/ADR-{number:04d}.md",
                applies_to=("src/**",),
                title="T" * 300,
                status="accepted",
                invariant="I" * 500,
                on_change=tuple("C" * 300 for _ in range(10)),
                evidence=None,
                origin_pr=None,
                supersedes=None,
                invariant_line=5,
                matched_path_count=1,
                metadata_hash="sha256:" + (f"{number:x}" * 64)[:64],
            )
            for number in range(10)
        )
        stored = repository_decision_context.loaded(
            base_sha="a" * 40,
            index_hash="sha256:" + ("f" * 64),
            decisions=decisions,
        )
        runtime = self._runtime()
        with (
            patch.object(review_tool_runtime, "postgres_runtime", return_value=runtime),
            patch.object(review_source_tools, "postgres_runtime", return_value=runtime),
            patch.object(review_tool_runtime.postgres_jobs, "require_live_lease"),
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=self._live_state(),
            ),
            patch.object(review_source_tools, "review_run_snapshot", return_value=pull),
            patch.object(
                review_run_application,
                "load_or_create_live_repository_guidance",
                return_value=repository_guidance_context.not_configured(
                    base_sha="a" * 40,
                ),
            ),
            patch.object(
                review_run_application,
                "load_or_create_live_repository_decisions",
                return_value=stored,
            ),
            patch.object(
                capacity,
                "current",
                return_value=capacity.CapacityLimits(
                    result_max_chars=7_000,
                    text_page_max_chars=1_000,
                ),
            ),
            patch.object(
                review_contract,
                "load_installed_contract",
                return_value=TEST_REVIEW_CONTRACT,
            ),
            patch.dict(
                os.environ,
                {"REVIEW_AGENT_PROFILE": "default-standard"},
                clear=True,
            ),
        ):
            result = json.loads(
                review_source_tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        self.assertEqual(result["run_id"], 41)
        self.assertEqual(result["repository_decisions_untrusted"]["status"], "unavailable")
        self.assertEqual(
            result["repository_decisions_untrusted"]["failure_code"],
            "decision_context_result_budget",
        )
        self.assertEqual(result["repository_decisions_untrusted"]["decisions"], [])

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
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        with (
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_source_tools,
                "postgres_runtime",
                return_value=self._runtime(),
            ),
            patch.object(
                review_run_application,
                "load_live_changed_file_page",
                return_value=page,
            ),
        ):
            result = json.loads(
                review_source_tools.pr_files.__wrapped__({"run_id": 41})
            )

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
                        review_source_tools,
                        "gateway_source_session",
                        return_value=source,
                    ),
                    patch.object(
                        review_source_tools,
                        "pull_request_identity",
                        return_value=(self.repository, 1, pull),
                    ),
                    patch.object(
                        review_source_tools,
                        "review_run_snapshot",
                        return_value=pull,
                    ),
                    patch.object(
                        review_source_tools,
                        "_pr_diff_from_patches",
                        return_value=json.dumps({"diff_source": "per_file_patch"}),
                    ) as fallback,
                ):
                    result = json.loads(
                        review_source_tools.pr_diff.__wrapped__({"run_id": 41})
                    )

                self.assertEqual(result["diff_source"], "per_file_patch")
                fallback.assert_called_once()

    def test_terminal_per_file_handoff_records_only_registered_paths(self) -> None:
        source = SimpleNamespace(run_id=41)
        registered = RunFile(
            path="missing.py",
            change_status="modified",
            previous_path="",
            domain="general",
            review_mode="normal",
            diff_state=review_run_application.DiffState.UNSEEN,
            is_changed_path=True,
        )
        cases = (
            ("complete", None, (), "not_in_changed_files"),
            ("complete", registered, ("missing.py",), "not_in_changed_files"),
            ("incomplete", registered, (), "not_in_changed_index"),
        )
        for index_state, item, expected, path_state in cases:
            index = SimpleNamespace(files=[], index_state=index_state)
            with (
                self.subTest(index_state=index_state, registered=item is not None),
                patch.object(
                    review_source_tools,
                    "_enumerate_changed_file_index",
                    return_value=index,
                ),
                patch.object(
                    review_run_application,
                    "lookup_live_run_file",
                    return_value=RunFileLookup(
                        item=item,
                        registration_complete=True,
                    ),
                ),
                patch.object(
                    review_run_application, "record_live_diff_result"
                ) as record,
                patch.object(
                    review_source_tools,
                    "postgres_runtime",
                    return_value=self._runtime(),
                ),
            ):
                result = json.loads(
                    review_source_tools._pr_diff_from_patches(
                        source=source,
                        repository=self.repository,
                        number=1,
                        run_id=41,
                        path="missing.py",
                        max_chars=10_000,
                        start_char=0,
                        reported=1,
                    )
                )

            exposure = record.call_args.args[2]
            self.assertEqual(exposure.unavailable_paths, expected)
            self.assertEqual(result["path_state"], path_state)
            self.assertEqual(result["unavailable_paths"], list(expected))
            self.assertTrue(result["terminal"])

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
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_source_tools,
                "postgres_runtime",
                return_value=self._runtime(),
            ),
            patch.object(
                review_run_application,
                "load_live_file_context",
                return_value=(snapshot, run_file),
            ),
            patch.object(
                review_run_application, "record_live_source_read"
            ) as record,
        ):
            result = json.loads(
                review_source_tools.pr_file.__wrapped__(
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
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools,
                "pull_request_identity",
                return_value=(self.repository, 1, pull),
            ),
            patch.object(
                review_source_tools,
                "postgres_runtime",
                return_value=self._runtime(),
            ),
            patch.object(
                review_run_application,
                "load_live_file_context",
                return_value=(snapshot, run_file),
            ),
        ):
            result = json.loads(
                review_source_tools.pr_file.__wrapped__(
                    {"run_id": 41, "path": "src/app.py", "side": "head"}
                )
            )

        self.assertEqual(
            result["error"], "GitHub gateway returned a different review subject"
        )

    def test_missing_worker_session_fails_before_source_or_database_work(self) -> None:
        with (
            patch.object(review_source_tools, "gateway_source_session") as source,
            patch.object(review_tool_runtime, "postgres_runtime") as runtime,
        ):
            result = json.loads(
                review_source_tools.review_begin({"existing_run_id": 41})
            )

        self.assertEqual(
            result["error"],
            "a live review worker lease is required; stop this review turn",
        )
        source.assert_not_called()
        runtime.assert_not_called()

    def test_every_model_handler_requires_a_worker_lease(self) -> None:
        handlers = (
            (review_source_tools.review_begin, {"existing_run_id": 41}),
            (review_source_tools.pr_files, {"run_id": 41}),
            (review_source_tools.pr_diff, {"run_id": 41}),
            (review_source_tools.pr_file, {"run_id": 41}),
            (review_memory_tools.review_memory_context, {"run_id": 41}),
            (review_memory_tools.review_memory_record, {"run_id": 41}),
            (review_delivery_tool.review_deliver, {"run_id": 41}),
        )
        for handler, args in handlers:
            with self.subTest(handler=handler.__name__):
                result = json.loads(handler(args))
                self.assertEqual(
                    result["error"],
                    "a live review worker lease is required; stop this review turn",
                )

    def test_memory_record_keeps_machine_metadata_out_of_visible_review(self) -> None:
        source = SimpleNamespace(run_id=41)
        pull = {"state": "open", "head": {"sha": "b" * 40}}
        with (
            patch.object(
                review_memory_tools,
                "gateway_source_session",
                return_value=source,
            ),
            patch.object(
                review_memory_tools,
                "pull_request_identity",
                return_value=(self.repository, 7, pull),
            ),
            patch.object(
                review_memory_tools,
                "postgres_runtime",
                return_value=self._runtime(),
            ),
            patch.object(review_run_application, "reopen_live_finding_collection"),
            patch.object(
                review_memory_tools,
                "review_run_snapshot",
                return_value=pull,
            ),
            patch.object(review_memory_tools, "load_changed_files", return_value=[]),
            patch.object(
                review_memory_tools.review_finding_application,
                "record_live_findings",
                return_value=SimpleNamespace(items=(), suggestions_recorded=0),
            ),
        ):
            result = json.loads(
                review_memory_tools.review_memory_record.__wrapped__(
                    {"run_id": 41, "findings": []}
                )
            )

        self.assertIn("only in hidden review metadata", result["instruction"])
        self.assertIn("do not put fingerprints in the visible review body", result["instruction"])

    def test_delivery_retries_a_recoverable_changed_path_coverage_gap(self) -> None:
        runtime = self._runtime()
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        pull = {"state": "open", "head": {"sha": "b" * 40}}
        coverage = CoverageSummary(
            state=CoverageState.INCOMPLETE,
            changed_files_reported=2,
            changed_files_registered=2,
            registration_complete=True,
            changed_paths_with_complete_diff=1,
            changed_paths_with_source_reads=1,
            supporting_context_paths_read=0,
            context_ranges_read=1,
            unseen_paths=1,
            unavailable_paths=0,
            truncated_paths=0,
        )
        with (
            patch.object(review_delivery_tool, "postgres_runtime", return_value=runtime),
            patch.object(
                review_delivery_tool, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_delivery_tool,
                "pull_request_identity",
                return_value=(self.repository, 7, pull),
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=SimpleNamespace(phase="reviewing"),
            ),
            patch.object(
                review_delivery_tool,
                "review_run_snapshot",
                return_value=pull,
            ),
            patch.object(
                review_run_application,
                "summarize_postgres_coverage",
                return_value=coverage,
            ),
            patch.object(
                review_delivery_tool.review_publication_application,
                "prepare_postgres_publication",
            ) as prepare,
        ):
            result = json.loads(
                review_delivery_tool.review_deliver.__wrapped__({"run_id": 41})
            )

        self.assertEqual(result["stage"], "validation_failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["changed_paths_registered"], 2)
        self.assertEqual(result["changed_paths_with_complete_diff"], 1)
        self.assertEqual(result["changed_paths_unseen"], 1)
        self.assertIn("diff_state is unseen", result["next_action"])
        self.assertIn("review_agent_pr_files", result["next_action"])
        self.assertIn("review_agent_pr_diff", result["next_action"])
        prepare.assert_not_called()

    def test_delivery_keeps_terminal_gaps_publishable(
        self,
    ) -> None:
        coverage = CoverageSummary(
            state=CoverageState.INCOMPLETE,
            changed_files_reported=2,
            changed_files_registered=2,
            registration_complete=True,
            changed_paths_with_complete_diff=1,
            changed_paths_with_source_reads=0,
            supporting_context_paths_read=0,
            context_ranges_read=0,
            unseen_paths=0,
            unavailable_paths=1,
            truncated_paths=0,
        )

        self.assertEqual(review_delivery_tool._recoverable_diff_gap(coverage), 0)

    def test_delivery_publishes_after_one_coverage_recovery_attempt(self) -> None:
        runtime = self._runtime()
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        pull = {"state": "open", "head": {"sha": "b" * 40}}
        coverage = CoverageSummary(
            state=CoverageState.INCOMPLETE,
            changed_files_reported=2,
            changed_files_registered=2,
            registration_complete=True,
            changed_paths_with_complete_diff=1,
            changed_paths_with_source_reads=1,
            supporting_context_paths_read=0,
            context_ranges_read=1,
            unseen_paths=1,
            unavailable_paths=0,
            truncated_paths=0,
        )
        prepared = SimpleNamespace(
            publication_id=81,
            findings_count=0,
            suggestions_count=0,
            resolved_count=0,
            ignored_previous_verdicts=(),
        )
        with (
            patch.object(review_delivery_tool, "postgres_runtime", return_value=runtime),
            patch.object(
                review_delivery_tool, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_delivery_tool,
                "pull_request_identity",
                return_value=(self.repository, 7, pull),
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=SimpleNamespace(phase="rendering"),
            ),
            patch.object(
                review_delivery_tool,
                "review_run_snapshot",
                return_value=pull,
            ),
            patch.object(
                review_run_application,
                "summarize_postgres_coverage",
                return_value=coverage,
            ) as summarize,
            patch.object(
                review_delivery_tool.settings.ReviewAgentSettings,
                "from_environment",
                return_value=SimpleNamespace(
                    feedback_enabled=False,
                    publish_max_bytes=100_000,
                    publication_max_attempts=3,
                ),
            ),
            patch.object(
                review_delivery_tool.review_publication_application,
                "prepare_postgres_publication",
                return_value=prepared,
            ) as prepare,
        ):
            result = json.loads(
                review_delivery_tool.review_deliver.__wrapped__({"run_id": 41})
            )

        self.assertEqual(result["stage"], "queued_for_publication")
        self.assertEqual(result["publication_id"], 81)
        summarize.assert_not_called()
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs["review_job_id"], 7)
        self.assertEqual(prepare.call_args.kwargs["review_lease_generation"], 3)

    def test_malformed_worker_session_fails_before_source_or_database_work(self) -> None:
        with (
            patch.object(review_source_tools, "gateway_source_session") as source,
            patch.object(review_tool_runtime, "postgres_runtime") as runtime,
        ):
            result = json.loads(
                review_source_tools.review_begin(
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
            patch.object(review_tool_runtime, "postgres_runtime", return_value=runtime),
            patch.object(review_source_tools, "gateway_source_session") as source,
            self.assertLogs("review_agent_tools.review_tool_runtime", level="ERROR"),
        ):
            result = json.loads(
                review_source_tools.review_begin(
                    {"existing_run_id": 41},
                    session_id=self.session_id,
                )
            )

        self.assertEqual(
            result["error"],
            "worker lease could not be verified; stop this review turn",
        )
        source.assert_not_called()

    def test_delivery_preserves_primary_error_when_failure_state_write_fails(
        self,
    ) -> None:
        runtime = self._runtime()
        source = SimpleNamespace(
            run_id=41,
            lease=SimpleNamespace(job_id=7, lease_generation=3),
        )
        pull = {"state": "open", "head": {"sha": "b" * 40}}
        secret_detail = "postgresql://operator:secret@example.test/reviews"
        with (
            patch.object(
                review_delivery_tool,
                "gateway_source_session",
                return_value=source,
            ),
            patch.object(
                review_delivery_tool,
                "pull_request_identity",
                return_value=(self.repository, 7, pull),
            ),
            patch.object(
                review_delivery_tool,
                "postgres_runtime",
                return_value=runtime,
            ),
            patch.object(
                review_run_application,
                "load_live_run_state",
                return_value=SimpleNamespace(phase="reviewing"),
            ),
            patch.object(
                review_delivery_tool,
                "review_run_snapshot",
                side_effect=review_tool_runtime.ToolInputError(
                    "primary review failure"
                ),
            ),
            patch.object(
                review_tool_runtime,
                "postgres_runtime",
                return_value=runtime,
            ),
            patch.object(
                review_tool_runtime.review_run_application,
                "fail_live_run",
                side_effect=RuntimeError(secret_detail),
            ),
            self.assertLogs(
                "review_agent_tools.review_tool_runtime",
                level="ERROR",
            ) as logged,
        ):
            result = json.loads(
                review_delivery_tool.review_deliver.__wrapped__({"run_id": 41})
            )

        self.assertEqual(result["error"], "primary review failure")
        rendered_logs = "\n".join(logged.output)
        self.assertIn("run_id=41", rendered_logs)
        self.assertIn("failure_code=review_deliver_error", rendered_logs)
        self.assertNotIn(secret_detail, rendered_logs)

    def test_plugin_manifest_and_registered_handlers_have_one_owner(self) -> None:
        registry = _FakeRegistry()
        with patch.object(
            review_agent_tools,
            "_installed_result_max_chars",
            return_value=160_000,
        ):
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
