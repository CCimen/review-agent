#!/usr/bin/env python3
"""Serve signed review requests and admit them to the durable queue."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import threading
from typing import Mapping, cast

import psycopg


def _load_package() -> None:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "admission.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools import admission  # noqa: E402
from review_agent_tools.github_webhook import verify_signature  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
    PostgreSQLRuntimeError,
)
from review_agent_tools.settings import SettingsError  # noqa: E402


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
    application: admission.AdmissionConfig
    github_app_max_body_bytes: int
    max_concurrent_requests: int
    request_timeout_seconds: int


def load_server_config(
    environment: Mapping[str, str] | None = None,
) -> AdmissionServerConfig:
    """Validate transport and application settings before opening PostgreSQL."""
    values = os.environ if environment is None else environment
    return AdmissionServerConfig(
        application=admission.load_config(values),
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
            self._write(200, admission.response_body("ok"))
            return
        if self.path == "/ready":
            try:
                body = admission.response_body(
                    str(admission.ready_check(server.config, server.runtime)["status"])
                )
                self._write(200, body)
            except Exception as exc:
                print(f"review admission readiness failed: {exc}", file=sys.stderr)
                self._write(503, admission.response_body("not_ready"))
            return
        self._write(404, admission.response_body("not_found"))

    def do_POST(self) -> None:
        server = self._server()
        if not server.acquire_request_slot():
            self._write(503, admission.response_body("busy"), retry_after="5")
            return
        try:
            self._admit(server)
        finally:
            server.release_request_slot()

    def _admit(self, server: "AdmissionServer") -> None:
        if self.path != admission.GITHUB_APP_PATH:
            self._write(404, admission.response_body("not_found"))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, admission.response_body("missing_length"))
            return
        if length < 0:
            self._write(411, admission.response_body("missing_length"))
            return
        if length > server.github_app_max_body_bytes:
            self._write(413, admission.response_body("payload_too_large"))
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
            self._write(401, admission.response_body("bad_signature"))
            return
        try:
            response = admission.receive_github_app_delivery(
                body=body,
                payload=admission.decode_request(body),
                delivery_id=self.headers.get("X-GitHub-Delivery", ""),
                event=self.headers.get("X-GitHub-Event", ""),
                config=server.config,
                runtime=server.runtime,
            )
            self._write(202, response.to_json())
        except admission.GitHubAppDeliveryConflict:
            self._write(409, admission.response_body("delivery_conflict"))
        except admission.GitHubAppPayloadTooLarge as exc:
            print(f"GitHub App webhook rejected: {exc}", file=sys.stderr)
            self._write(413, admission.response_body("payload_too_large"))
        except (admission.AdmissionError, SettingsError) as exc:
            print(f"GitHub App webhook rejected: {exc}", file=sys.stderr)
            self._write(400, admission.response_body("bad_request"))
        except (PostgreSQLRuntimeError, psycopg.Error) as exc:
            print(f"GitHub App webhook database failure: {exc}", file=sys.stderr)
            self._write(503, admission.response_body("database_unavailable"))
        except Exception as exc:
            print(f"GitHub App webhook internal failure: {exc}", file=sys.stderr)
            self._write(500, admission.response_body("internal_error"))

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
        config: admission.AdmissionConfig,
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
    server_config = load_server_config()
    config = server_config.application
    runtime = PostgreSQLRuntime(
        config.database_url, role=PostgreSQLRuntimeRole.ADMISSION
    )
    runtime.open()
    server = AdmissionServer(
        (host, port),
        config=config,
        runtime=runtime,
        github_app_max_body_bytes=server_config.github_app_max_body_bytes,
        max_concurrent_requests=server_config.max_concurrent_requests,
        request_timeout_seconds=server_config.request_timeout_seconds,
    )
    print(f"Review Agent admission listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "verify-config"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=admission.DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.command == "verify-config":
        config = load_server_config().application
        runtime = PostgreSQLRuntime(
            config.database_url, role=PostgreSQLRuntimeRole.ADMISSION
        )
        runtime.open()
        try:
            admission.ready_check(config, runtime)
            print("ok")
            return 0
        finally:
            runtime.close()
    serve(str(args.host), int(args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
