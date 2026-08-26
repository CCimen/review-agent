#!/usr/bin/env python3
"""HTTP entrypoint for the deterministic review-feedback bridge."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType
from typing import Protocol, cast

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_REQUESTS = 8
CONFIG_ENV_NAMES = (
    "REVIEW_AGENT_FEEDBACK_WEBHOOK_SECRET",
    "REVIEW_AGENT_FEEDBACK_GH_TOKEN",
    "REVIEW_AGENT_ALLOWED_REPOSITORIES",
    "REVIEW_AGENT_FEEDBACK_ALLOWED_ACTOR_IDS",
    "REVIEW_AGENT_DATABASE_URL",
)
DIAGNOSTIC_ENV_NAMES = CONFIG_ENV_NAMES + (
    "GH_TOKEN",
)


class FeedbackBridgeModule(Protocol):
    DEFAULT_PATH: str
    DEFAULT_PORT: int
    MAX_BODY_BYTES: int
    BridgeError: type[Exception]
    GitHubError: type[Exception]
    GitHubNotFound: type[Exception]
    UnauthorizedFeedback: type[Exception]

    def load_config(self) -> object: ...
    def ready_check(
        self, config: object, runtime: object
    ) -> Mapping[str, object]: ...
    def verify_signature(self, body: bytes, signature: str, secret: str) -> bool: ...
    def decode_request_body(self, body: bytes) -> object: ...
    def process_feedback(
        self,
        *,
        payload: object,
        config: object,
        github: object,
        runtime: object,
    ) -> "BridgeResponseLike": ...
    def response_body(self, status: str, message: str = "") -> bytes: ...


class BridgeResponseLike(Protocol):
    def to_json(self) -> bytes: ...


class GitHubClientClass(Protocol):
    def __call__(self, token: str) -> object: ...


class ConfigLike(Protocol):
    secret: str
    token: str
    database_url: object


class RuntimeLike(Protocol):
    def open(self) -> object: ...
    def close(self) -> None: ...


class RuntimeClass(Protocol):
    def __call__(self, database_url: object, *, role: object) -> RuntimeLike: ...


def _import_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def plugin_parent_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = [
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    ]
    candidates.extend(Path(entry) for entry in sys.path if entry)
    return tuple(candidates)


def _insert_plugin_parent() -> Path:
    for candidate in plugin_parent_candidates():
        if (candidate / "review_agent_tools" / "feedback_bridge.py").exists():
            sys.path.insert(0, str(candidate))
            return candidate
    raise SystemExit("Could not locate the review_agent_tools plugin")


def _module_is_from_parent(module: ModuleType, parent: Path) -> bool:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str) or not raw:
        return False
    try:
        path = Path(raw).resolve()
        root = parent.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _evict_stale_plugin_modules(parent: Path) -> None:
    for name in ("review_agent_tools.feedback_bridge", "review_agent_tools"):
        module = sys.modules.get(name)
        if module is not None and not _module_is_from_parent(module, parent):
            sys.modules.pop(name, None)


def _describe_bridge_source(module: ModuleType, parent: Path) -> None:
    raw = getattr(module, "__file__", "unknown")
    print(
        f"feedback bridge plugin source: {raw} (parent={parent})",
        file=sys.stderr,
        flush=True,
    )


def load_feedback_bridge() -> FeedbackBridgeModule:
    parent = _insert_plugin_parent()
    _evict_stale_plugin_modules(parent)
    module = _import_module("review_agent_tools.feedback_bridge")
    _describe_bridge_source(module, parent)
    return cast(FeedbackBridgeModule, module)


def env_presence_summary() -> str:
    states: list[str] = []
    for name in DIAGNOSTIC_ENV_NAMES:
        state = "set" if os.environ.get(name, "").strip() else "missing"
        states.append(f"{name}={state}")
    return ", ".join(states)


def _exit_code(exc: SystemExit) -> int:
    return exc.code if isinstance(exc.code, int) and exc.code != 0 else 1


def load_config_or_explain(bridge: FeedbackBridgeModule) -> ConfigLike:
    try:
        return cast(ConfigLike, bridge.load_config())
    except SystemExit as exc:
        message = str(exc.code) if exc.code else "configuration failed"
        print(
            f"feedback bridge configuration error: {message}",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"feedback bridge environment: {env_presence_summary()}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(_exit_code(exc)) from None


class FeedbackRequestHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        server = cast(FeedbackServer, self.server)
        self.connection.settimeout(server.request_timeout_seconds)

    def do_GET(self) -> None:
        bridge, config, _, runtime = self._state()
        if self.path == "/health":
            self._write(200, b'{"status":"ok"}')
            return
        if self.path == "/ready":
            try:
                self._write(200, _json_body(bridge.ready_check(config, runtime)))
            except Exception as exc:
                print(f"feedback bridge readiness failed: {exc}", file=sys.stderr)
                self._write(503, b'{"status":"not_ready"}')
            return
        self._write(404, bridge.response_body("not_found"))

    def do_POST(self) -> None:
        server = cast(FeedbackServer, self.server)
        if not server.acquire_request_slot():
            bridge, _, _, _ = self._state()
            self._write(503, bridge.response_body("busy"))
            return
        try:
            self._do_POST()
        finally:
            server.release_request_slot()

    def _do_POST(self) -> None:
        bridge, config, github, runtime = self._state()
        if self.path != bridge.DEFAULT_PATH:
            self._write(404, bridge.response_body("not_found"))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, bridge.response_body("missing_length"))
            return
        if length < 0:
            self._write(411, bridge.response_body("missing_length"))
            return
        if length > bridge.MAX_BODY_BYTES:
            self._write(413, bridge.response_body("payload_too_large"))
            return
        body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256", "")
        if not bridge.verify_signature(body, signature, config.secret):
            self._write(401, bridge.response_body("bad_signature"))
            return
        if self.headers.get("X-GitHub-Event", "") != "issue_comment":
            self._write(400, bridge.response_body("unsupported_event"))
            return
        try:
            response = bridge.process_feedback(
                payload=bridge.decode_request_body(body),
                config=config,
                github=github,
                runtime=runtime,
            )
            self._write(200, response.to_json())
        except bridge.UnauthorizedFeedback:
            self._write(200, bridge.response_body("unauthorized"))
        except bridge.GitHubNotFound as exc:
            self._write(200, bridge.response_body("not_found", str(exc)))
        except bridge.GitHubError as exc:
            self._write(502, bridge.response_body("github_error", str(exc)))
        except bridge.BridgeError as exc:
            self._write(400, bridge.response_body("bad_request", str(exc)))

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr)

    def _state(
        self,
    ) -> tuple[FeedbackBridgeModule, ConfigLike, object, RuntimeLike]:
        server = cast(FeedbackServer, self.server)
        return server.bridge, server.config, server.github, server.runtime

    def _write(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FeedbackServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        bridge: FeedbackBridgeModule,
        config: ConfigLike,
        github: object,
        runtime: RuntimeLike,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
    ) -> None:
        super().__init__(server_address, FeedbackRequestHandler)
        self.bridge = bridge
        self.config = config
        self.github = github
        self.runtime = runtime
        self.request_timeout_seconds = request_timeout_seconds
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)

    def acquire_request_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def release_request_slot(self) -> None:
        self._request_slots.release()


def serve(
    host: str,
    port: int,
    bridge: FeedbackBridgeModule | None = None,
) -> None:
    bridge = bridge or load_feedback_bridge()
    config = load_config_or_explain(bridge)
    github_client_class = cast(
        GitHubClientClass,
        getattr(bridge, "GitHubApiClient"),
    )
    runtime_class = cast(RuntimeClass, getattr(bridge, "PostgreSQLRuntime"))
    runtime_role = getattr(getattr(bridge, "PostgreSQLRuntimeRole"), "FEEDBACK")
    runtime = runtime_class(config.database_url, role=runtime_role)
    runtime.open()
    server = FeedbackServer(
        (host, port),
        bridge=bridge,
        config=config,
        github=github_client_class(config.token),
        runtime=runtime,
    )
    print(f"Review agent feedback bridge listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    bridge = load_feedback_bridge()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "verify-config"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=bridge.DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.command == "verify-config":
        config = load_config_or_explain(bridge)
        runtime_class = cast(RuntimeClass, getattr(bridge, "PostgreSQLRuntime"))
        runtime_role = getattr(getattr(bridge, "PostgreSQLRuntimeRole"), "FEEDBACK")
        runtime = runtime_class(config.database_url, role=runtime_role)
        runtime.open()
        try:
            bridge.ready_check(config, runtime)
            print("ok")
            return 0
        finally:
            runtime.close()
    serve(
        str(args.host),
        int(args.port),
        bridge,
    )
    return 0


def _json_body(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
