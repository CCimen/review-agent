from __future__ import annotations

from dataclasses import dataclass
import sys
import unittest
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import repository_guidance_context  # noqa: E402
from review_agent_tools.github.source import ReviewFilePage  # noqa: E402


BASE_SHA = "a" * 40
REPOSITORY = "example-org/example-repository"


@dataclass(frozen=True, slots=True)
class _Lease:
    job_id: int
    lease_generation: int


class _FileClient:
    def __init__(self) -> None:
        self.handler = Mock()

    def get_review_file_page(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
        path: str,
        side: Literal["base"],
        start_line: int,
        max_lines: int,
        max_chars: int,
    ) -> ReviewFilePage:
        return cast(
            ReviewFilePage,
            self.handler(
                run_id=run_id,
                job_id=job_id,
                lease_generation=lease_generation,
                path=path,
                side=side,
                start_line=start_line,
                max_lines=max_lines,
                max_chars=max_chars,
            ),
        )


@dataclass(frozen=True, slots=True)
class _Source:
    run_id: int
    lease: _Lease
    client: _FileClient


def page(content: str, *, state: str = "ok") -> ReviewFilePage:
    lines = content.splitlines()
    return ReviewFilePage(
        state=state,
        repository=REPOSITORY,
        revision=BASE_SHA,
        start_line=1,
        total_lines=len(lines),
        content=(
            "\n".join(
                f"{line_number}: {line}"
                for line_number, line in enumerate(lines, start=1)
            )
            if state == "ok"
            else ""
        ),
        complete_lines=len(lines) if state == "ok" else 0,
        partial_line=False,
    )


class RepositoryGuidanceContextTests(unittest.TestCase):
    def source(self) -> _Source:
        return _Source(
            run_id=41,
            lease=_Lease(job_id=7, lease_generation=3),
            client=_FileClient(),
        )

    def test_missing_config_is_an_explicit_not_configured_snapshot(self) -> None:
        source = self.source()
        source.client.handler.return_value = page(
            "", state="not_found_at_revision"
        )

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "not_configured")
        self.assertIsNone(result.instructions)
        self.assertEqual(result.context_files, ())
        self.assertRegex(result.snapshot_hash, r"^sha256:[0-9a-f]{64}$")

    def test_loads_fixed_instructions_then_only_explicit_context_in_order(self) -> None:
        source = self.source()
        config = """version = 1
context = ["context/platform.md", "context/backend.md"]
"""
        source.client.handler.side_effect = [
            page(config),
            page("Review for clear ownership and bounded failure modes."),
            page("The platform uses a shared authentication boundary."),
            page("Backend writes must use one transaction."),
        ]

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "loaded")
        self.assertEqual(result.instructions.path, ".review-agent/instructions.md")
        self.assertEqual(
            [item.path for item in result.context_files],
            [
                ".review-agent/context/platform.md",
                ".review-agent/context/backend.md",
            ],
        )
        self.assertEqual(
            [item.content for item in result.context_files],
            [
                "The platform uses a shared authentication boundary.",
                "Backend writes must use one transaction.",
            ],
        )
        self.assertEqual(
            [item.kwargs["path"] for item in source.client.handler.call_args_list],
            [
                ".review-agent/config.toml",
                ".review-agent/instructions.md",
                ".review-agent/context/platform.md",
                ".review-agent/context/backend.md",
            ],
        )

    def test_disabled_config_does_not_read_instructions_or_context(self) -> None:
        source = self.source()
        source.client.handler.return_value = page(
            "version = 1\nenabled = false\ncontext = ['context/ignored.md']"
        )

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "disabled")
        self.assertEqual(source.client.handler.call_count, 1)

    def test_missing_configured_file_rejects_the_whole_optional_context(self) -> None:
        source = self.source()
        source.client.handler.side_effect = [
            page("version = 1\ncontext = ['context/platform.md']"),
            page("", state="not_found_at_revision"),
            page("", state="not_found_at_revision"),
        ]

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "guidance_context_file_missing")
        self.assertIsNone(result.instructions)
        self.assertEqual(result.context_files, ())

    def test_combined_content_uses_one_explicit_budget(self) -> None:
        source = self.source()
        source.client.handler.side_effect = [
            page("version = 1\ncontext = ['context/platform.md']"),
            page("123456"),
            page("abcdef"),
        ]

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10,
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "guidance_content_budget_exceeded")
        self.assertIsNone(result.instructions)
        self.assertEqual(result.context_files, ())

    def test_invalid_utf8_config_is_invalid_at_runtime(self) -> None:
        source = self.source()
        source.client.handler.return_value = page("", state="not_utf8")

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "guidance_config_not_utf8")

    def test_oversized_config_is_invalid_at_runtime(self) -> None:
        source = self.source()
        source.client.handler.return_value = page("", state="too_large")

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=10_000,
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "guidance_config_too_large")

    def test_guidance_capacity_cannot_exceed_the_snapshot_storage_contract(self) -> None:
        source = self.source()
        source.client.handler.side_effect = [
            page("version = 1"),
            page("x" * (repository_guidance_context.MAX_GUIDANCE_CONTENT_CHARS + 1)),
        ]

        result = repository_guidance_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            content_max_chars=repository_guidance_context.MAX_GUIDANCE_CONTENT_CHARS
            + 10_000,
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "guidance_content_budget_exceeded")

    def test_payload_states_only_the_provenance_the_runtime_proves(self) -> None:
        context = repository_guidance_context.loaded(
            base_sha=BASE_SHA,
            config_hash="sha256:" + "b" * 64,
            instructions=None,
            context_files=(),
        )

        instruction = repository_guidance_context.payload(context)["instruction"]

        self.assertIn("read from the exact base snapshot", instruction)
        self.assertNotIn("code-reviewed", instruction)

    def test_snapshot_round_trip_detects_tampering(self) -> None:
        context = repository_guidance_context.loaded(
            base_sha=BASE_SHA,
            config_hash="sha256:" + "b" * 64,
            instructions=repository_guidance_context.guidance_file(
                ".review-agent/instructions.md",
                "Review changed behavior, not style preferences.",
            ),
            context_files=(
                repository_guidance_context.guidance_file(
                    ".review-agent/context/platform.md",
                    "The platform owns authentication.",
                ),
            ),
        )
        value = repository_guidance_context.snapshot_value(context)

        restored = repository_guidance_context.restore_snapshot(
            snapshot_id=9,
            value=value,
            expected_hash=context.snapshot_hash,
        )
        self.assertEqual(restored.snapshot_hash, context.snapshot_hash)
        self.assertEqual(restored.context_files, context.context_files)

        cast(dict[str, object], value)["status"] = "disabled"
        with self.assertRaises(
            repository_guidance_context.RepositoryGuidanceContextError
        ):
            repository_guidance_context.restore_snapshot(
                snapshot_id=9,
                value=value,
                expected_hash=context.snapshot_hash,
            )


if __name__ == "__main__":
    unittest.main()
