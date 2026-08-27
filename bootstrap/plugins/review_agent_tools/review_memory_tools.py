"""Finding-history context and append-only finding observation tools."""

from __future__ import annotations

from typing import Any, cast

from . import (
    capacity,
    review_finding_application,
    review_run_application,
    schemas,
)
from .domain.review import ReviewRunId
from .github.gateway import GitHubGatewayError
from .postgres.runtime import PostgreSQLRuntimeError
from .review_source_tools import load_changed_files
from .review_tool_runtime import (
    JsonObject,
    ReviewRunTerminal,
    ToolInputError,
    error_output,
    gateway_source_session,
    output_json,
    parse_path,
    postgres_runtime,
    pull_request_identity,
    pull_head_sha,
    review_run_snapshot,
    run_subject,
    run_terminal_payload,
    worker_lease_fence,
)


@worker_lease_fence()
def review_memory_context(args: dict[str, Any], **context: Any) -> str:
    try:
        source = gateway_source_session(args, context)
        repository, pr_number, _ = pull_request_identity(source)
        raw_paths_value = args.get("paths", [])
        if not isinstance(raw_paths_value, list):
            raise ToolInputError("paths must be an array")
        raw_paths = cast(list[Any], raw_paths_value)
        if len(raw_paths) > schemas.CHANGED_FILE_PAGE_MAX_ITEMS:
            raise ToolInputError(
                f"paths exceeds {schemas.CHANGED_FILE_PAGE_MAX_ITEMS} entries"
            )
        paths = [parse_path(item) for item in raw_paths]
        return output_json(
            review_finding_application.load_live_context(
                postgres_runtime(),
                review_finding_application.FindingContextQuery(
                    repository=repository,
                    paths=tuple(paths),
                    pr_number=pr_number,
                )
            )
        )
    except (
        ToolInputError,
        review_finding_application.ReviewFindingError,
        PostgreSQLRuntimeError,
    ) as exc:
        return error_output(str(exc))
    except Exception:
        return error_output("unexpected memory read failure")


@worker_lease_fence()
def review_memory_record(args: dict[str, Any], **context: Any) -> str:
    try:
        source = gateway_source_session(args, context)
        repository, number, initial_pull = pull_request_identity(source)
        run_id = source.run_id
        head_sha = pull_head_sha(initial_pull)
        findings_value = args.get("findings", [])
        if not isinstance(findings_value, list):
            raise ToolInputError("findings must be an array")
        findings = cast(list[Any], findings_value)

        # Render validation may send the same run back for a bounded finding
        # correction. Reopen that exact lifecycle edge before validating GitHub.
        review_run_application.reopen_live_finding_collection(
            postgres_runtime(),
            run_subject(
                repository=repository,
                pr_number=number,
                run_id=run_id,
            ),
            expected_head_sha=head_sha,
        )
        # The run-owned head is checked before GitHub I/O, then current GitHub state
        # is matched to that same snapshot. A fabricated model SHA remains a hard error.
        pull = review_run_snapshot(
            source=source,
            repository=repository,
            pr_number=number,
            phase="reviewing",
        )
        head_sha = pull_head_sha(pull)
        if pull.get("state") != "open":
            raise ToolInputError("the pull request is no longer open")
        files = load_changed_files(source)
        # Honest-partial recording: when GitHub reports more changed files than were
        # enumerated (e.g. a PR beyond the files-API ceiling), record findings for the
        # files that WERE enumerated rather than hard-refusing the whole review.
        # Findings on un-enumerated paths are still rejected below, and incomplete
        # coverage is surfaced by the renderer's "Review context incomplete" banner —
        # the review is never silently dropped nor falsely reported clean.
        changed_by_path = {str(item.get("path", "")): item for item in files}
        finding_objects: list[JsonObject] = []
        for raw_finding in findings:
            if not isinstance(raw_finding, dict):
                raise ToolInputError("each finding must be an object")
            finding = cast(JsonObject, raw_finding)
            finding_objects.append(finding)
            finding_path = parse_path(finding.get("path"))
            if finding_path not in changed_by_path:
                raise ToolInputError(
                    "every recorded finding must point to a changed pull-request file"
                )

        changed_file_records = tuple(
            review_finding_application.ChangedFile(
                path=path,
                context_hash=str(item.get("context_hash", "")),
                context_hash_source=str(item.get("context_hash_source", "")),
                patch=(
                    patch_value
                    if isinstance((patch_value := item.get("patch")), str)
                    else None
                ),
            )
            for path, item in changed_by_path.items()
        )
        def load_head_file(path: str, start_line: int, end_line: int) -> str | None:
            try:
                page = source.client.get_review_file_page(
                    run_id=source.run_id,
                    job_id=source.lease.job_id,
                    lease_generation=source.lease.lease_generation,
                    path=path,
                    side="head",
                    start_line=start_line,
                    max_lines=end_line - start_line + 1,
                    max_chars=capacity.DEFAULT_RESULT_MAX_CHARS,
                )
                expected_lines = end_line - start_line + 1
                if (
                    page.state != "ok"
                    or page.revision != head_sha
                    or page.partial_line
                    or page.complete_lines != expected_lines
                ):
                    return None
                numbered = page.content.splitlines()
                if len(numbered) != expected_lines:
                    return None
                trusted: list[str] = []
                for line_number, line in enumerate(numbered, start=start_line):
                    prefix = f"{line_number}: "
                    if not line.startswith(prefix):
                        return None
                    trusted.append(line[len(prefix) :])
                return "\n".join(trusted)
            except (ToolInputError, GitHubGatewayError):
                return None

        result = review_finding_application.record_live_findings(
            postgres_runtime(),
            run_id=ReviewRunId(run_id),
            head_sha=head_sha,
            raw_findings=finding_objects,
            changed_files=changed_file_records,
            head_file_loader=load_head_file,
        )
        return output_json(
            {
                "recorded": [
                    {
                        "finding_id": item.finding_id,
                        "occurrence_id": item.occurrence_id,
                        "fingerprint": item.fingerprint,
                        "fingerprint_short": item.fingerprint[:12],
                        "local_reference": item.local_reference,
                        "context_hash": item.context_hash,
                        "suppressed": item.suppressed,
                        "decision": item.decision,
                        "suggestion_status": item.suggestion_status,
                    }
                    for item in result.items
                ],
                "suggestions_recorded": result.suggestions_recorded,
                "instruction": (
                    "Omit every item whose suppressed field is true. Use fingerprint_short "
                    "only in hidden review metadata for each published item; do not put "
                    "fingerprints in the visible review body. An omitted optional suggestion "
                    "does not invalidate its finding; continue to delivery and leave that "
                    "finding in the coding-agent brief."
                ),
            }
        )
    except ReviewRunTerminal as terminal:
        return output_json(run_terminal_payload(terminal.run_id))
    except (
        ToolInputError,
        review_finding_application.ReviewFindingError,
        PostgreSQLRuntimeError,
    ) as exc:
        return error_output(str(exc))
    except Exception:
        return error_output("unexpected memory write failure")
