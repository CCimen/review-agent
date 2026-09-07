"""Optional code locations, fenced by the existing review source authority."""

from __future__ import annotations

import logging
from typing import Any

from . import capacity
from .code_graph_client import request_graph
from .code_graph_contract import GraphError, GraphIdentity, QUERY_PATTERNS
from .github.gateway import GitHubGatewayError
from .review_tool_runtime import (
    GatewaySourceSession, ReviewRunTerminal, ToolInputError, error_output,
    gateway_source_session, output_json, pull_head_sha, pull_request_identity,
    json_object_or_empty, review_run_snapshot, run_terminal_payload, source_error, worker_lease_fence,
)
from .settings import ReviewAgentSettings, SettingsError

logger = logging.getLogger(__name__)


def _identity(source: GatewaySourceSession) -> GraphIdentity:
    return GraphIdentity(source.run_id, source.lease.job_id, source.lease.lease_generation)


def prepare_graph(source: GatewaySourceSession) -> dict[str, object] | None:
    try:
        url = ReviewAgentSettings.from_environment().code_graph_url
        if url is None:
            return None
        return request_graph(url, _identity(source), operation="prepare")
    except Exception as exc:
        logger.warning("Code graph preparation unavailable: %s", type(exc).__name__)
        return {"status": "unavailable"}


@worker_lease_fence()
def related_code(args: dict[str, Any], **context: Any) -> str:
    run_id = 0
    try:
        source = gateway_source_session(args, context)
        run_id = source.run_id
        pattern, target = args.get("pattern"), args.get("target")
        if (set(args) != {"run_id", "pattern", "target"}
                or not isinstance(pattern, str) or pattern not in QUERY_PATTERNS
                or not isinstance(target, str) or not 1 <= len(target.strip()) <= 512):
            raise ToolInputError("use a supported graph pattern and a target of at most 512 characters")
        url = ReviewAgentSettings.from_environment().code_graph_url
        if url is None:
            return output_json({"status": "disabled", "next_action": "Continue with the normal source tools."})
        repository, number, _ = pull_request_identity(source)
        pull = review_run_snapshot(source=source, repository=repository, pr_number=number, phase="reviewing")
        expected_sha = pull_head_sha(pull)
        expected_repository_id = json_object_or_empty(json_object_or_empty(pull.get("head")).get("repo")).get("id")
        result = request_graph(url, _identity(source), operation="query", pattern=pattern, target=target)
        review_run_snapshot(
            source=source, repository=repository, pr_number=number,
            phase="reviewing", expected_head_sha=expected_sha,
        )
        if result.get("head_sha") != expected_sha or result.get("repository_id") != expected_repository_id:
            raise GraphError("graph revision does not match the review")
        rendered = output_json({
            **result, "context_trust": "untrusted", "pattern": pattern,
            "next_action": (
                "Read candidate paths with review_agent_pr_file at head before using them as evidence. "
                "Unresolved relationships and test links are hints and do not prove calls or coverage. "
                "For ambiguous names, query a candidate's symbol to narrow the target. "
                "If graph context is unavailable, continue the review with normal source tools; do not poll."
            ),
        })
        if len(rendered) > capacity.current().result_max_chars:
            raise GraphError("graph response exceeds the tool result budget")
        return rendered
    except ReviewRunTerminal:
        return output_json(run_terminal_payload(run_id))
    except GitHubGatewayError as exc:
        return error_output(str(source_error(exc)))
    except ToolInputError as exc:
        return error_output(str(exc))
    except (GraphError, SettingsError):
        return output_json({"status": "unavailable", "next_action": "Continue with the normal source tools."})
