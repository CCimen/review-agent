from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import capacity, repository_decision_context  # noqa: E402
from review_agent_tools.github.gateway import GitHubGatewayRetryable  # noqa: E402
from review_agent_tools.github.source import ReviewFilePage  # noqa: E402


BASE_SHA = "a" * 40
REPOSITORY = "example-org/example-repository"


@dataclass(frozen=True, slots=True)
class _Lease:
    job_id: int
    lease_generation: int


class _DecisionFileClient:
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
    client: _DecisionFileClient


def page(path: str, content: str, *, state: str = "ok") -> ReviewFilePage:
    lines = content.splitlines()
    numbered = "\n".join(
        f"{line_number}: {line}" for line_number, line in enumerate(lines, start=1)
    )
    return ReviewFilePage(
        state=state,
        repository=REPOSITORY,
        revision=BASE_SHA,
        start_line=1,
        total_lines=len(lines),
        content=numbered if state == "ok" else "",
        complete_lines=len(lines) if state == "ok" else 0,
        partial_line=False,
    )


def prefix_page(content: str, *, returned_lines: int) -> ReviewFilePage:
    lines = content.splitlines()
    prefix = lines[:returned_lines]
    return ReviewFilePage(
        state="ok",
        repository=REPOSITORY,
        revision=BASE_SHA,
        start_line=1,
        total_lines=len(lines),
        content="\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(prefix, start=1)
        ),
        complete_lines=len(prefix),
        partial_line=False,
    )


class RepositoryDecisionContextTests(unittest.TestCase):
    def source(self) -> _Source:
        return _Source(
            run_id=41,
            lease=_Lease(job_id=7, lease_generation=3),
            client=_DecisionFileClient(),
        )

    def test_missing_index_is_an_explicit_not_configured_context(self) -> None:
        source = self.source()
        source.client.handler.return_value = page(
            ".review-agent/decisions.toml",
            "",
            state="not_found_at_revision",
        )

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/rag/config.py",),
        )

        self.assertEqual(result.status, "not_configured")
        self.assertIsNone(result.failure_code)
        self.assertEqual(result.decisions, ())
        self.assertRegex(result.snapshot_hash, r"^sha256:[0-9a-f]{64}$")

    def test_loads_only_matching_adr_headers_from_the_exact_base_revision(self) -> None:
        source = self.source()
        index = """version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["src/rag/**"]

[[decision]]
id = "ADR-0008"
adr_path = "docs/decisions/ADR-0008.md"
applies_to = ["src/api/**"]
"""
        adr = """+++
id = "ADR-0007"
title = "Keep the retrieval budget coupled"
status = "accepted"
invariant = "Chunk size, overlap, and top-k form one budget."
on_change = ["Run the retrieval evaluation."]
+++
"""
        source.client.handler.side_effect = [
            page(".review-agent/decisions.toml", index),
            page("docs/decisions/ADR-0007.md", adr),
        ]

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/rag/nested/config.py",),
        )

        self.assertEqual(result.status, "loaded")
        self.assertEqual([item.id for item in result.decisions], ["ADR-0007"])
        self.assertEqual(result.decisions[0].matched_path_count, 1)
        self.assertRegex(result.index_hash or "", r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(source.client.handler.call_count, 2)
        for call in source.client.handler.call_args_list:
            self.assertEqual(call.kwargs["side"], "base")
            self.assertEqual(
                call.kwargs["max_chars"],
                capacity.DEFAULT_RESULT_MAX_CHARS,
            )

    def test_adr_body_may_continue_after_the_bounded_header_window(self) -> None:
        source = self.source()
        index = """version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["src/rag/**"]
"""
        adr = "\n".join(
            (
                "+++",
                'id = "ADR-0007"',
                'title = "Keep the retrieval budget coupled"',
                'status = "accepted"',
                'invariant = "Chunk size, overlap, and top-k form one budget."',
                'on_change = ["Run the retrieval evaluation."]',
                "+++",
                "# Context",
                *(f"Human rationale line {number}." for number in range(1, 193)),
            )
        )
        source.client.handler.side_effect = [
            page(".review-agent/decisions.toml", index),
            prefix_page(adr, returned_lines=60),
        ]

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/rag/config.py",),
        )

        self.assertEqual(result.status, "loaded")
        self.assertEqual([item.id for item in result.decisions], ["ADR-0007"])
        self.assertEqual(result.decisions[0].invariant_line, 5)

    def test_too_many_matches_returns_no_partial_decision_set(self) -> None:
        source = self.source()
        entries = "\n".join(
            f"""[[decision]]
id = "ADR-{number:04d}"
adr_path = "docs/decisions/ADR-{number:04d}.md"
applies_to = ["src/**"]"""
            for number in range(11)
        )
        source.client.handler.return_value = page(
            ".review-agent/decisions.toml",
            f"version = 1\n{entries}",
        )

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/app.py",),
        )

        self.assertEqual(result.status, "too_many_matches")
        self.assertEqual(result.failure_code, "decision_match_limit_exceeded")
        self.assertEqual(result.decisions, ())
        self.assertEqual(source.client.handler.call_count, 1)

    def test_one_invalid_adr_rejects_the_whole_evidence_set(self) -> None:
        source = self.source()
        index = """version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["src/**"]

[[decision]]
id = "ADR-0008"
adr_path = "docs/decisions/ADR-0008.md"
applies_to = ["src/**"]
"""
        valid = """+++
id = "ADR-0007"
title = "Accepted decision"
status = "accepted"
invariant = "Keep the accepted contract."
on_change = ["Run the contract test."]
+++
"""
        invalid = """+++
id = "ADR-0008"
title = "Broken decision"
status = "accepted"
invariant = "Missing on-change evidence."
+++
"""
        source.client.handler.side_effect = [
            page(".review-agent/decisions.toml", index),
            page("docs/decisions/ADR-0007.md", valid),
            page("docs/decisions/ADR-0008.md", invalid),
        ]

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/app.py",),
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure_code, "decision_adr_invalid")
        self.assertEqual(result.decisions, ())

    def test_gateway_failure_degrades_optional_context(self) -> None:
        source = self.source()
        source.client.handler.side_effect = GitHubGatewayRetryable(
            "github_503"
        )

        result = repository_decision_context.load(
            source,
            repository=REPOSITORY,
            base_sha=BASE_SHA,
            changed_paths=("src/app.py",),
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.failure_code, "decision_source_unavailable")
        self.assertEqual(result.decisions, ())


if __name__ == "__main__":
    unittest.main()
