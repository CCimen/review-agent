"""Expose only provider connection operations from Hermes's loopback dashboard."""

from __future__ import annotations

import hmac
import json
import os
import re
import signal
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import cast

from .hermes_control import HermesControlClient, HermesControlError, HermesRuntimeClient
from .model_accounts import ModelProvider

_SESSION_ROUTE = re.compile(
    r"/api/providers/oauth/(?:openai-codex/poll/|sessions/)([A-Za-z0-9_-]{22,80})"
)
_QUOTA_ROUTE = re.compile(r"/api/quota/(openai-codex|anthropic)(\?refresh=true)?")


class ProviderGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        control: HermesControlClient,
        runtime: HermesRuntimeClient | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("Provider control token is required")
        self.token = token
        self.control = control
        self.runtime = runtime
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, ProviderHandler)

    def process_request(
        self, request: socket | tuple[bytes, socket], client_address: tuple[str, int]
    ) -> None:
        if not isinstance(request, socket):
            raise TypeError("Provider gateway requires a TCP socket")
        request.settimeout(5)
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: socket | tuple[bytes, socket], client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class ProviderHandler(BaseHTTPRequestHandler):
    @property
    def gateway(self) -> ProviderGateway:
        return cast(ProviderGateway, self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self) -> None:
        if self.path == "/healthz" and self.command == "GET":
            self._reply(200, {"status": "ready"})
            return
        token = self.headers.get("X-Hermes-Session-Token", "")
        if not hmac.compare_digest(token.encode(), self.gateway.token.encode()):
            self._reply(401, {"error": "Unauthorized"})
            return
        if (
            self.headers.get("Transfer-Encoding")
            or self.headers.get("Content-Length", "0") != "0"
        ):
            self._reply(400, {"error": "Request body is not supported"})
            return
        control = self.gateway.control
        try:
            if self.command == "GET" and self.path in {
                "/api/runtime",
                "/api/managed-runtime",
            }:
                if self.gateway.runtime is None:
                    self._reply(
                        503, {"error": "Hermes runtime diagnostics are not configured"}
                    )
                    return
                if self.path == "/api/runtime":
                    payload: object = asdict(self.gateway.runtime.status())
                else:
                    managed = self.gateway.runtime.managed_status()
                    payload = {
                        **asdict(managed),
                        "instance_id": str(managed.instance_id),
                    }
            elif self.command == "GET" and (
                quota_match := _QUOTA_ROUTE.fullmatch(self.path)
            ):
                if self.gateway.runtime is None:
                    self._reply(
                        503, {"error": "Hermes runtime diagnostics are not configured"}
                    )
                    return
                quota = self.gateway.runtime.quota(
                    ModelProvider(quota_match.group(1)),
                    refresh=bool(quota_match.group(2)),
                )
                payload = {**asdict(quota), "instance_id": str(quota.instance_id)}
            elif self.command == "GET" and self.path == "/api/providers/oauth":
                providers = control.provider_statuses()
                payload = {
                    "providers": [
                        {
                            "id": provider.provider,
                            "status": {
                                "logged_in": provider.connected,
                                "expires_at": provider.expires_at.isoformat()
                                if provider.expires_at
                                else None,
                            },
                        }
                        for provider in providers
                    ]
                }
            elif self.command == "GET" and self.path == "/api/model/options":
                models = control.models()
                payload = {
                    "providers": [
                        {
                            "id": provider,
                            "models": [
                                model.model
                                for model in models
                                if model.provider == provider
                            ],
                        }
                        for provider in ("openai-codex", "anthropic")
                    ]
                }
            elif (
                self.command == "POST"
                and self.path == "/api/providers/oauth/openai-codex/start"
            ):
                payload = asdict(control.start_codex_login())
            elif match := _SESSION_ROUTE.fullmatch(self.path):
                session_id = match.group(1)
                if self.command == "GET" and "/poll/" in self.path:
                    payload = {"status": control.poll_codex_login(session_id).status}
                elif self.command == "DELETE" and "/sessions/" in self.path:
                    payload = {"ok": control.cancel_codex_login(session_id).cancelled}
                else:
                    self._reply(404, {"error": "Unknown operation"})
                    return
            else:
                self._reply(404, {"error": "Unknown operation"})
                return
        except HermesControlError:
            self._reply(502, {"error": "Hermes provider control is unavailable"})
            return
        self._reply(200, payload)


def main() -> None:
    token = os.environ.get("REVIEW_AGENT_HERMES_CONTROL_TOKEN", "")
    control = HermesControlClient("http://127.0.0.1:9119", token)
    api_key = os.environ.get("API_SERVER_KEY", "")
    runtime = HermesRuntimeClient("http://127.0.0.1:8642", api_key) if api_key else None
    with ProviderGateway(
        ("0.0.0.0", 9120), token=token, control=control, runtime=runtime
    ) as server:

        def shutdown(_signal: int, _frame: object) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        server.serve_forever()


if __name__ == "__main__":
    main()
