#!/usr/bin/env python3
"""Serve the private deterministic Review Agent GitHub gateway."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
from typing import Mapping, cast

import psycopg


def _load_package() -> None:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "github" / "gateway.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools.github.app_auth import (  # noqa: E402
    GitHubAppConfigurationError,
    ReviewReadTokenService,
    load_private_key_file,
)
from review_agent_tools.github.gateway import (  # noqa: E402
    AUTHORIZE_REVIEW_DELIVERY_PATH,
    READ_REVIEW_SOURCE_PATH,
    DeliveryLeaseIdentity,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    ReviewSourceRequest,
    ReviewGitHubGateway,
)
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


logger = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 4_096
_DEFAULT_MAX_CONCURRENT_REQUESTS = 16
_REQUEST_TIMEOUT_SECONDS = 40


class GitHubGatewayConfigurationError(ValueError):
    """The private gateway cannot start from its supplied configuration."""


def _positive_integer(name: str, default: int | None = None) -> int:
    raw_default = "" if default is None else str(default)
    raw = os.environ.get(name, raw_default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GitHubGatewayConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise GitHubGatewayConfigurationError(f"{name} must be positive")
    return value


def _private_key() -> str:
    raw_path = os.environ.get("REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE", "").strip()
    if not raw_path:
        raise GitHubGatewayConfigurationError(
            "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE is required"
        )
    try:
        return load_private_key_file(raw_path)
    except GitHubAppConfigurationError as exc:
        raise GitHubGatewayConfigurationError(str(exc)) from exc


class GatewayRequestHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_REQUEST_TIMEOUT_SECONDS)

    def do_GET(self) -> None:
        server = self._server()
        if self.path == "/health":
            self._write(200, {"status": "ok"})
            return
        if self.path == "/ready":
            try:
                server.runtime.readiness()
            except (PostgreSQLRuntimeError, psycopg.Error):
                self._write(503, {"status": "not_ready"})
                return
            self._write(200, {"status": "ready"})
            return
        self._write(404, {"reason": "not_found"})

    def do_POST(self) -> None:
        server = self._server()
        if self.path not in {
            AUTHORIZE_REVIEW_DELIVERY_PATH,
            READ_REVIEW_SOURCE_PATH,
        }:
            self._write(404, {"reason": "not_found"})
            return
        if not server.acquire_request_slot():
            self._write(503, {"reason": "github_gateway_busy"})
            return
        try:
            if self.path == AUTHORIZE_REVIEW_DELIVERY_PATH:
                self._authorize(server)
            else:
                self._read_source(server)
        finally:
            server.release_request_slot()

    def _authorize(self, server: "GatewayServer") -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, {"reason": "missing_length"})
            return
        if length < 1:
            self._write(411, {"reason": "missing_length"})
            return
        if length > _MAX_REQUEST_BYTES:
            self._write(413, {"reason": "payload_too_large"})
            return
        try:
            decoded = json.loads(self.rfile.read(length))
            if not isinstance(decoded, dict):
                raise GitHubGatewayProtocolError("gateway request must be an object")
            identity = DeliveryLeaseIdentity.from_mapping(
                cast(Mapping[str, object], decoded)
            )
            authorized = server.gateway.authorize_review_delivery(
                delivery_id=identity.delivery_id,
                lease_owner=identity.lease_owner,
                lease_generation=identity.lease_generation,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, GitHubGatewayProtocolError):
            self._write(400, {"reason": "invalid_gateway_request"})
            return
        except GitHubGatewayRejected as exc:
            self._write(409, {"reason": exc.reason})
            return
        except GitHubGatewayRetryable as exc:
            self._write(503, {"reason": exc.reason})
            return
        except (PostgreSQLRuntimeError, psycopg.Error):
            self._write(503, {"reason": "github_gateway_database_unavailable"})
            return
        except Exception as exc:
            logger.error(
                "GitHub gateway operation failed: %s", type(exc).__name__
            )
            self._write(500, {"reason": "github_gateway_internal_error"})
            return
        self._write(200, authorized.to_mapping())

    def _read_source(self, server: "GatewayServer") -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, {"reason": "missing_length"})
            return
        if length < 1:
            self._write(411, {"reason": "missing_length"})
            return
        if length > _MAX_REQUEST_BYTES:
            self._write(413, {"reason": "payload_too_large"})
            return
        try:
            decoded = json.loads(self.rfile.read(length))
            if not isinstance(decoded, dict):
                raise GitHubGatewayProtocolError("gateway request must be an object")
            request = ReviewSourceRequest.from_mapping(
                cast(Mapping[str, object], decoded)
            )
            result = server.gateway.read_review_source(request)
        except (json.JSONDecodeError, UnicodeDecodeError, GitHubGatewayProtocolError):
            self._write(400, {"reason": "invalid_gateway_request"})
            return
        except GitHubGatewayRejected as exc:
            self._write(409, {"reason": exc.reason})
            return
        except GitHubGatewayRetryable as exc:
            self._write(503, {"reason": exc.reason})
            return
        except (PostgreSQLRuntimeError, psycopg.Error):
            self._write(503, {"reason": "github_gateway_database_unavailable"})
            return
        except Exception as exc:
            logger.error(
                "GitHub gateway source operation failed: %s", type(exc).__name__
            )
            self._write(500, {"reason": "github_gateway_internal_error"})
            return
        self._write(200, result.to_mapping())

    def _server(self) -> "GatewayServer":
        return cast(GatewayServer, self.server)

    def _write(self, status: int, value: Mapping[str, object]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logger.info(format, *args)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        gateway: ReviewGitHubGateway,
        runtime: PostgreSQLRuntime,
        max_concurrent_requests: int,
    ) -> None:
        super().__init__(address, GatewayRequestHandler)
        self.gateway = gateway
        self.runtime = runtime
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)

    def acquire_request_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def release_request_slot(self) -> None:
        self._request_slots.release()


def serve(host: str, port: int) -> None:
    settings = ReviewAgentSettings.from_environment()
    app_id = _positive_integer("REVIEW_AGENT_GITHUB_APP_ID")
    private_key = _private_key()
    runtime = PostgreSQLRuntime(
        settings.postgres_database_url,
        role=PostgreSQLRuntimeRole.ADMISSION,
    )
    runtime.open()
    server = GatewayServer(
        (host, port),
        gateway=ReviewGitHubGateway(
            postgres=runtime,
            tokens=ReviewReadTokenService(
                app_id=app_id,
                private_key_pem=private_key,
                postgres=runtime,
            ),
            profile=settings.profile,
        ),
        runtime=runtime,
        max_concurrent_requests=_positive_integer(
            "REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS",
            _DEFAULT_MAX_CONCURRENT_REQUESTS,
        ),
    )

    def request_stop(_signal: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Review Agent GitHub gateway listening on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8646)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        serve(str(args.host), int(args.port))
    except GitHubGatewayConfigurationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
