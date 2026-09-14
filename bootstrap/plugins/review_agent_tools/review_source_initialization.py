"""Initialize one leased review's exact source snapshot and changed-file inventory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import cast

from . import changed_files, review_contract, review_run_application
from .github.gateway import GitHubGatewayError
from .postgres.coverage import FileIndexSummary
from .postgres.runtime import PostgreSQLRuntime
from .review_tool_runtime import (
    GatewaySourceSession,
    JsonObject,
    SHA_RE,
    ToolInputError,
    load_application_snapshot,
    pull_request_identity,
    pull_snapshot,
    run_subject,
    source_error,
)


@dataclass(frozen=True, slots=True)
class InitializedReview:
    repository: str
    pr_number: int
    subject: review_run_application.RunSubject
    pull: JsonObject
    file_index: FileIndexSummary
    changed_files_reported: int
    phase: review_run_application.RunPhase
    started_at: str


def enumerate_changed_file_index(
    source: GatewaySourceSession,
    *,
    reported: int,
    maximum: int = changed_files.GITHUB_PR_FILES_LIMIT,
) -> changed_files.ChangedFileIndex:
    # ChangedFilePager owns offset-safe enumeration and its honest coverage state.
    # This adapter replaces only the transport with the fixed App gateway operation.
    def request_page(per_page: int, page: int) -> tuple[bytes, bool, dict[str, str]]:
        try:
            result = source.client.get_changed_files_page(
                run_id=source.run_id,
                job_id=source.lease.job_id,
                lease_generation=source.lease.lease_generation,
                per_page=per_page,
                page=page,
            )
        except GitHubGatewayError as exc:
            raise source_error(exc) from exc
        if result.state != "ok":
            raise ToolInputError("GitHub changed-file page is unavailable")
        return result.body, result.truncated, result.headers

    return changed_files.enumerate_changed_files(
        request_page, reported=reported, max_files=maximum
    )


def load_changed_files(
    source: GatewaySourceSession,
    maximum: int = changed_files.GITHUB_PR_FILES_LIMIT,
) -> list[JsonObject]:
    """Load trusted changed-file context for source and finding tools."""
    index = enumerate_changed_file_index(
        source,
        reported=0,
        maximum=maximum,
    )
    files: list[JsonObject] = []
    for entry in index.files:
        blob_sha = entry["blob_sha"]
        patch_text = entry["patch"] or ""
        is_blob = bool(SHA_RE.fullmatch(blob_sha))
        # GitHub normally supplies the file blob SHA. If it does not, keep a
        # deterministic patch hash as a diagnostic value; the persistence path
        # will fall back to the authoritative PR head SHA for safe suppression.
        context_hash = (
            blob_sha
            if is_blob
            else hashlib.sha256(
                (
                    f"{entry['path']}\n{entry['status']}\n"
                    f"{entry['additions']}\n{entry['deletions']}\n{patch_text}"
                ).encode("utf-8")
            ).hexdigest()
        )
        files.append(
            {
                "path": entry["path"],
                "status": entry["status"],
                "previous_path": entry["previous_path"],
                "additions": entry["additions"],
                "deletions": entry["deletions"],
                "changes": entry["changes"],
                "patch_available": bool(patch_text),
                "patch": entry["patch"],
                "patch_state": entry["patch_state"],
                "context_hash": context_hash,
                "context_hash_source": "blob" if is_blob else "patch",
            }
        )
    return files


def initialize_review(
    source: GatewaySourceSession,
    runtime: PostgreSQLRuntime,
    installed_contract: review_contract.ReviewContract,
    *,
    pull_identity: tuple[str, int, JsonObject] | None = None,
) -> InitializedReview:
    """Validate and resume source collection without invoking a model or graph."""
    repository, number, pull = pull_identity or pull_request_identity(source)
    subject = run_subject(repository=repository, pr_number=number, run_id=source.run_id)
    persisted = review_run_application.load_live_run_state(runtime, subject)
    review_contract.require_matching_execution_contract(
        cast(JsonObject, json.loads(persisted.resolved_config.canonical_json)),
        installed_contract,
    )
    if pull.get("state") != "open":
        raise ToolInputError("the pull request is no longer open")

    def snapshot(phase: review_run_application.RunPhase) -> JsonObject:
        result = load_application_snapshot(
            lambda: review_run_application.load_live_snapshot(
                runtime,
                subject,
                phase=phase,
                pull_loader=lambda: pull_snapshot(pull_request_identity(source)[2]),
            )
        )
        return result.pull

    phase = persisted.phase
    pull = snapshot(phase)
    if phase == "accepted":
        review_run_application.advance_live_phase(runtime, subject, "fetching_pr")
        phase = "fetching_pr"

    file_index = persisted.file_index
    if not file_index.registration_complete and phase in {
        "fetching_pr",
        "collecting_diff",
    }:
        files = load_changed_files(source)
        try:
            reported = int(pull.get("changed_files") or 0)
        except (TypeError, ValueError):
            reported = 0
        changed_files_reported = max(reported, len(files))
        file_index = review_run_application.register_live_changed_files(
            runtime,
            subject,
            files=cast(list[dict[str, object]], files),
            changed_files_reported=changed_files_reported,
        )
    else:
        changed_files_reported = file_index.changed_files_reported or 0

    if phase == "fetching_pr":
        pull = snapshot("collecting_diff")
        phase = "collecting_diff"

    return InitializedReview(
        repository=repository,
        pr_number=number,
        subject=subject,
        pull=pull,
        file_index=file_index,
        changed_files_reported=changed_files_reported,
        phase=phase,
        started_at=persisted.started_at,
    )
