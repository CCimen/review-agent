"""The worker's client for one review turn on the pinned Hermes API server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from http.client import HTTPException
import json
from pathlib import Path
import re
from typing import cast
from urllib import error, parse, request

from ..postgres import jobs
from ..review import contract as review_contract
from ..review.turn import ClaimedReview, ReviewTurnError, turn_messages
from ..https import SameOriginHttpsRedirectHandler
from ..postgres.telemetry import TokenUsage
from .control import MANAGED_REVIEW_PATH

_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,59}")


class HermesChatConfigurationError(ValueError):
    """The Hermes chat endpoint, token or installed review skill is unusable."""


@dataclass(frozen=True, slots=True)
class HermesChatSettings:
    endpoint: str
    bearer_token: str = field(repr=False)
    skill_path: Path
    documentation_skill_path: Path | None = None

    def __post_init__(self) -> None:
        parsed = parse.urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path != MANAGED_REVIEW_PATH or parsed.query or parsed.fragment
        ):
            raise HermesChatConfigurationError(
                f"Hermes endpoint must be an HTTP URL ending in {MANAGED_REVIEW_PATH}"
            )
        if not self.bearer_token.strip():
            raise HermesChatConfigurationError("Hermes bearer token is required")
        if not self.skill_path.is_file():
            raise HermesChatConfigurationError(
                f"review skill does not exist: {self.skill_path}"
            )
        if self.documentation_skill_path is not None and not self.documentation_skill_path.is_file():
            raise HermesChatConfigurationError("documentation review skill does not exist")


class HermesChatClient:
    """Small OpenAI-compatible client for the pinned Hermes chat boundary.

    Hermes loads SOUL.md and AGENTS.md natively for API turns. The chat
    endpoint has no route-skill selector, so this client layers only the
    installed review skill body as the ephemeral system message.
    """

    def __init__(
        self,
        settings: HermesChatSettings,
        *,
        opener: request.OpenerDirector | None = None,
    ) -> None:
        self._settings = settings
        self._system_instructions = _load_skill_instructions(settings.skill_path)
        self._documentation_instructions = (
            _load_skill_instructions(settings.documentation_skill_path)
            if settings.documentation_skill_path is not None else None
        )
        self._opener = opener or request.build_opener(
            SameOriginHttpsRedirectHandler()
        )

    def review(
        self, claimed: ClaimedReview, *, timeout: timedelta
    ) -> TokenUsage | None:
        session = jobs.WorkerLeaseSession(
            job_id=claimed.job.id,
            lease_generation=claimed.job.lease_generation,
        )
        contract = review_contract.queued_contract(claimed.resolved_config)
        instructions, request_text = turn_messages(
            claimed,
            code_instructions=self._system_instructions,
            documentation_instructions=self._documentation_instructions,
        )
        payload = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": request_text},
                ],
                "stream": False,
                "provider": contract.model_provider,
                "model": contract.model,
                "model_options": {"reasoning": {
                    "enabled": contract.reasoning_effort != "none",
                    "effort": contract.reasoning_effort,
                }},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        call = request.Request(
            self._settings.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._settings.bearer_token}",
                "Content-Type": "application/json",
                # Hermes idempotency deduplicates transport retries only within
                # this lease. PostgreSQL generation fences own reclaim safety.
                "Idempotency-Key": session.encode(),
                "X-Hermes-Session-Id": session.encode(),
            },
        )
        try:
            # PostgreSQL remains authoritative for completion. Optional usage is
            # bounded; malformed or unreadable telemetry cannot undo publication.
            with self._opener.open(call, timeout=timeout.total_seconds()) as response:
                try:
                    body = response.read(1024 * 1024 + 1)
                except (OSError, ValueError, HTTPException):
                    return None
            if len(body) > 1024 * 1024:
                return None
            usage = parse_usage(body)
            unfinished = parse_turn_failure(body)
            if unfinished is not None:
                code, detail = unfinished
                raise ReviewTurnError(
                    "Hermes reported an unfinished review turn",
                    retryable=True, code=code, detail=detail, usage=usage,
                )
            return usage
        except error.HTTPError as exc:
            retryable = (
                exc.code
                in {
                    HTTPStatus.REQUEST_TIMEOUT,
                    HTTPStatus.TOO_EARLY,
                    HTTPStatus.TOO_MANY_REQUESTS,
                }
                or exc.code >= 500
            )
            try:
                error_body = exc.read(64 * 1024)
            except (OSError, ValueError, HTTPException):
                error_body = b""
            finally:
                exc.close()
            error_code, error_message = parse_error_body(error_body)
            agent_incomplete = error_code == "agent_incomplete"
            raise ReviewTurnError(
                f"Hermes returned HTTP {exc.code}",
                retryable=retryable,
                code="hermes_agent_error" if agent_incomplete else f"hermes_http_{exc.code}",
                # Hermes redacts its agent failure text; other bodies keep
                # only their machine-readable code.
                detail=(
                    error_message
                    if agent_incomplete
                    else error_code
                    if error_code is not None and _ERROR_CODE.fullmatch(error_code)
                    else None
                ),
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, TimeoutError) or (
                isinstance(exc, error.URLError) and isinstance(exc.reason, TimeoutError)
            )
            raise ReviewTurnError(
                "Hermes request did not complete",
                retryable=True,
                code="hermes_timeout" if timed_out else "hermes_unreachable",
            ) from exc


def _load_skill_instructions(path: Path) -> str:
    """Load the skill body used as the worker's ephemeral system message."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return content
    marker = content.find("\n---\n", 4)
    if marker < 0:
        raise HermesChatConfigurationError(
            f"review skill has unterminated YAML frontmatter: {path}"
        )
    return content[marker + len("\n---\n") :].lstrip()


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return cast(dict[str, object], payload) if isinstance(payload, dict) else None


def parse_turn_failure(body: bytes) -> tuple[str, str | None] | None:
    """Return (code, redacted detail) when Hermes reports an unfinished turn.

    Hermes marks a failed or truncated agent run in a 200 response with a
    ``hermes`` block and ``finish_reason`` "error" or "length"; its ``error``
    text is redacted by Hermes before it leaves the API server.
    """
    payload = _json_object(body)
    if payload is None:
        return None
    raw_hermes = payload.get("hermes")
    hermes = cast(dict[str, object], raw_hermes) if isinstance(raw_hermes, dict) else {}
    finish_reason: object = None
    raw_choices = payload.get("choices")
    if isinstance(raw_choices, list) and raw_choices:
        first = cast(list[object], raw_choices)[0]
        if isinstance(first, dict):
            finish_reason = cast(dict[str, object], first).get("finish_reason")
    unfinished = (
        hermes.get("failed") is True
        or hermes.get("completed") is False
        or finish_reason == "error"
        or finish_reason == "length"
    )
    if not unfinished:
        return None
    truncated = (
        hermes.get("error_code") == "output_truncated" or finish_reason == "length"
    )
    detail = hermes.get("error")
    return (
        "hermes_output_truncated" if truncated else "hermes_agent_error",
        detail if isinstance(detail, str) else None,
    )


def parse_error_body(body: bytes) -> tuple[str | None, str | None]:
    """Return the (code, message) of an OpenAI-style error envelope."""
    payload = _json_object(body)
    raw_error = payload.get("error") if payload is not None else None
    if not isinstance(raw_error, dict):
        return None, None
    envelope = cast(dict[str, object], raw_error)
    code = envelope.get("code")
    message = envelope.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


def parse_usage(body: bytes) -> TokenUsage | None:
    payload = _json_object(body)
    if payload is None:
        return None
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    usage = cast(dict[str, object], raw_usage)
    values: list[int] = []
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is not int or not 0 <= value <= 9223372036854775807:
            return None
        values.append(value)
    prompt, completion, total = values
    if total <= 0 or prompt + completion != total:
        return None
    return TokenUsage(prompt, completion, total)
