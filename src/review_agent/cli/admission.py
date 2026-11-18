"""Serve signed review requests and admit them to the durable queue."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
from typing import cast

import psycopg

from ..github.webhook import (
    CommandKind,
    GitHubWebhookError,
    UnsupportedGitHubEvent,
    normalize_event,
    verify_signature,
)
from ..review import contract as review_contract
from ..postgres import webhook_deliveries
from ..postgres.deployment_settings import apply_at_startup
from ..postgres.runtime import (
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
    PostgreSQLRuntimeError,
)
from ..settings import PostgresDatabaseUrl, ReviewAgentSettings, SettingsError


GITHUB_APP_PATH = "/webhooks/github-app"
DEFAULT_PORT = 8644

class AdmissionError(ValueError):
    """The signed request does not satisfy the admission contract."""


class GitHubAppDeliveryConflict(AdmissionError):
    """A GitHub delivery GUID was reused for different immutable input."""


class GitHubAppPayloadTooLarge(AdmissionError):
    """A signed App delivery cannot fit the durable normalized envelope."""


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    database_url: PostgresDatabaseUrl = field(repr=False)
    profile: str
    github_app_secret: str = field(repr=False)
    contract_environment: Mapping[str, str] = field(repr=False, compare=False)
    webhook_delivery_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class WebhookReceiptResponse:
    status: str

    def to_json(self) -> bytes:
        return response_body(self.status)


def load_config(environment: Mapping[str, str] | None = None) -> AdmissionConfig:
    values = dict(environment if environment is not None else os.environ)
    settings = ReviewAgentSettings(values)
    github_app_secret = values.get(
        "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET", ""
    ).strip()
    if not github_app_secret:
        raise SettingsError("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET is required")
    return AdmissionConfig(
        database_url=settings.postgres_database_url,
        profile=settings.profile,
        github_app_secret=github_app_secret,
        contract_environment=review_contract.deployment_environment(values),
        webhook_delivery_max_attempts=_positive_integer(
            values, "REVIEW_AGENT_WEBHOOK_DELIVERY_MAX_ATTEMPTS", 3
        ),
    )


def decode_request(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError("request body must be valid UTF-8 JSON") from exc


def ready_check(config: AdmissionConfig, runtime: PostgreSQLRuntime) -> dict[str, str]:
    if config.database_url != runtime.database_url:
        raise AdmissionError("admission runtime does not match its configured database")
    runtime.readiness()
    _admission_contract(config.profile, config.contract_environment)
    return {"status": "ready"}


def _admission_contract(
    profile: str, environment: Mapping[str, str]
) -> review_contract.ReviewContract:
    try:
        contract = review_contract.load_packaged_contract(
            profile,
            environment=environment,
        )
    except review_contract.ReviewContractError as exc:
        raise AdmissionError(str(exc)) from exc
    if contract.profile != profile:
        raise AdmissionError("configured profile does not match the packaged reviewer")
    return contract


def receive_github_app_delivery(
    *,
    body: bytes,
    payload: object,
    delivery_id: str,
    event: str,
    config: AdmissionConfig,
    runtime: PostgreSQLRuntime,
) -> WebhookReceiptResponse:
    """Normalize and durably commit one App delivery without external I/O."""
    try:
        normalized = normalize_event(event, payload)
    except UnsupportedGitHubEvent:
        return WebhookReceiptResponse("ignored")
    except GitHubWebhookError as exc:
        raise AdmissionError("GitHub webhook payload is invalid") from exc
    if normalized.event == "installation":
        category = webhook_deliveries.CommandCategory.INSTALLATION
    elif normalized.event == "installation_repositories":
        category = webhook_deliveries.CommandCategory.REPOSITORY_ACCESS
    elif normalized.command_kind is CommandKind.REVIEW:
        category = webhook_deliveries.CommandCategory.REVIEW
    elif normalized.command_kind in {
        CommandKind.FINDING_FEEDBACK,
        CommandKind.QUALITY_FEEDBACK,
        CommandKind.INVALID,
    }:
        category = webhook_deliveries.CommandCategory.FEEDBACK
    else:
        category = webhook_deliveries.CommandCategory.IGNORED

    try:
        with runtime.transaction() as connection:
            # GitHub's delivery deadline is ten seconds. Leave time for pool
            # checkout, commit, and the HTTP response instead of inheriting the
            # runtime's broader workload timeout.
            connection.execute("SET LOCAL statement_timeout = '5s'")
            registered = webhook_deliveries.register_delivery(
                connection,
                definition=webhook_deliveries.DeliveryDefinition(
                    delivery_guid=delivery_id,
                    event=normalized.event,
                    action=normalized.action,
                    payload_sha256=hashlib.sha256(body).hexdigest(),
                    provider_installation_id=(
                        normalized.provider_installation_id
                    ),
                    provider_repository_id=normalized.provider_repository_id,
                    repository_full_name=normalized.repository,
                    command_category=category,
                    normalized_schema_version=normalized.schema_version,
                    normalized_payload=normalized.normalized,
                ),
                max_attempts=config.webhook_delivery_max_attempts,
            )
    except webhook_deliveries.DeliveryConflict as exc:
        raise GitHubAppDeliveryConflict(str(exc)) from exc
    except webhook_deliveries.NormalizedPayloadTooLarge as exc:
        raise GitHubAppPayloadTooLarge(str(exc)) from exc
    except webhook_deliveries.WebhookDeliveryError as exc:
        raise AdmissionError(str(exc)) from exc

    status = (
        "duplicate"
        if isinstance(registered, webhook_deliveries.DuplicateDelivery)
        else "received"
    )
    return WebhookReceiptResponse(status)


def response_body(status: str) -> bytes:
    return json.dumps(
        {"status": status}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


GITHUB_APP_MAX_BODY_BYTES = 2_097_152


def _positive_integer(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < 1:
        raise SettingsError(f"{name} must be positive")
    return value


def _bounded_positive_integer(
    environment: Mapping[str, str], name: str, default: int, maximum: int
) -> int:
    value = _positive_integer(environment, name, default)
    if value > maximum:
        raise SettingsError(f"{name} must not exceed {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class AdmissionServerConfig:
    application: AdmissionConfig
    github_app_max_body_bytes: int
    max_concurrent_requests: int
    request_timeout_seconds: int


def load_server_config(
    environment: Mapping[str, str] | None = None,
) -> AdmissionServerConfig:
    """Validate transport and application settings before opening PostgreSQL."""
    values = os.environ if environment is None else environment
    return AdmissionServerConfig(
        application=load_config(values),
        github_app_max_body_bytes=_bounded_positive_integer(
            values,
            "REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES",
            GITHUB_APP_MAX_BODY_BYTES,
            GITHUB_APP_MAX_BODY_BYTES,
        ),
        max_concurrent_requests=_positive_integer(
            values, "REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS", 8
        ),
        request_timeout_seconds=_positive_integer(
            values, "REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS", 30
        ),
    )


class AdmissionRequestHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        server = self._server()
        self.connection.settimeout(server.request_timeout_seconds)

    def do_GET(self) -> None:
        server = self._server()
        if self.path == "/health":
            self._write(200, response_body("ok"))
            return
        if self.path == "/ready":
            try:
                body = response_body(
                    str(ready_check(server.config, server.runtime)["status"])
                )
                self._write(200, body)
            except Exception as exc:
                print(
                    "review admission readiness failed: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                )
                self._write(503, response_body("not_ready"))
            return
        self._write(404, response_body("not_found"))

    def do_POST(self) -> None:
        server = self._server()
        if not server.acquire_request_slot():
            self._write(503, response_body("busy"), retry_after="5")
            return
        try:
            self._admit(server)
        finally:
            server.release_request_slot()

    def _admit(self, server: "AdmissionServer") -> None:
        if self.path != GITHUB_APP_PATH:
            self._write(404, response_body("not_found"))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, response_body("missing_length"))
            return
        if length < 0:
            self._write(411, response_body("missing_length"))
            return
        if length > server.github_app_max_body_bytes:
            self._write(413, response_body("payload_too_large"))
            return
        body = self.rfile.read(length)
        self._receive_github_app(server, body)

    def _receive_github_app(
        self, server: "AdmissionServer", body: bytes
    ) -> None:
        if not verify_signature(
            body,
            self.headers.get("X-Hub-Signature-256", ""),
            server.config.github_app_secret,
        ):
            self._write(401, response_body("bad_signature"))
            return
        try:
            response = receive_github_app_delivery(
                body=body,
                payload=decode_request(body),
                delivery_id=self.headers.get("X-GitHub-Delivery", ""),
                event=self.headers.get("X-GitHub-Event", ""),
                config=server.config,
                runtime=server.runtime,
            )
            self._write(202, response.to_json())
        except GitHubAppDeliveryConflict:
            self._write(409, response_body("delivery_conflict"))
        except GitHubAppPayloadTooLarge as exc:
            print(
                f"GitHub App webhook rejected: {type(exc).__name__}",
                file=sys.stderr,
            )
            self._write(413, response_body("payload_too_large"))
        except (AdmissionError, SettingsError) as exc:
            print(
                f"GitHub App webhook rejected: {type(exc).__name__}",
                file=sys.stderr,
            )
            self._write(400, response_body("bad_request"))
        except (PostgreSQLRuntimeError, psycopg.Error) as exc:
            print(
                f"GitHub App webhook database failure: {type(exc).__name__}",
                file=sys.stderr,
            )
            self._write(503, response_body("database_unavailable"))
        except Exception as exc:
            print(
                f"GitHub App webhook internal failure: {type(exc).__name__}",
                file=sys.stderr,
            )
            self._write(500, response_body("internal_error"))

    def _server(self) -> "AdmissionServer":
        return cast(AdmissionServer, self.server)

    def _write(self, status: int, body: bytes, *, retry_after: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr)


class AdmissionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        config: AdmissionConfig,
        runtime: PostgreSQLRuntime,
        github_app_max_body_bytes: int,
        max_concurrent_requests: int,
        request_timeout_seconds: int,
    ) -> None:
        super().__init__(address, AdmissionRequestHandler)
        self.config = config
        self.runtime = runtime
        self.github_app_max_body_bytes = github_app_max_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)

    def acquire_request_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def release_request_slot(self) -> None:
        self._request_slots.release()


def serve(host: str, port: int) -> None:
    config = load_config()
    runtime = PostgreSQLRuntime(
        config.database_url, role=PostgreSQLRuntimeRole.ADMISSION
    )
    runtime.open()
    try:
        apply_at_startup(runtime, "webhook")
        server_config = load_server_config()
        with AdmissionServer(
            (host, port),
            config=server_config.application,
            runtime=runtime,
            github_app_max_body_bytes=server_config.github_app_max_body_bytes,
            max_concurrent_requests=server_config.max_concurrent_requests,
            request_timeout_seconds=server_config.request_timeout_seconds,
        ) as server:
            print(f"Review Agent admission listening on {host}:{port}", flush=True)
            server.serve_forever()
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "verify-config"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.command == "verify-config":
        config = load_server_config().application
        runtime = PostgreSQLRuntime(
            config.database_url, role=PostgreSQLRuntimeRole.ADMISSION
        )
        runtime.open()
        try:
            ready_check(config, runtime)
            print("ok")
            return 0
        finally:
            runtime.close()
    serve(str(args.host), int(args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
