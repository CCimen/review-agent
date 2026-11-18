"""Serve optional code graph context in a container without credential mounts."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import threading
from typing import cast

from ..code_graph.contract import (
    GRAPH_QUERY_PATH,
    GRAPH_RESULT_MAX_BYTES,
    GraphError,
)

# The indexer runs in the graph environment, which holds the parsers and this
# package but none of the server's dependencies. The service starts it as that
# environment's copy of this command with --indexer.
GRAPH_PYTHON = Path("/opt/review-agent-code-graph/bin/python")
GRAPH_RUNNER = GRAPH_PYTHON.with_name("review-agent-code-graph")


def serve(host: str, port: int) -> None:
    # The indexer runs with only its isolated dependency set, so server imports
    # stay out of that process's startup path.
    from ..code_graph.service import CodeGraphService
    from ..github.gateway import GitHubGatewayError
    from ..settings import ReviewAgentSettings

    configured = ReviewAgentSettings.from_environment()
    if not GRAPH_PYTHON.is_file():
        raise GraphError("the code graph runtime is not installed")
    service = CodeGraphService(
        root=Path(os.environ.get("REVIEW_AGENT_CODE_GRAPH_ROOT", "/var/lib/review-agent-code-graph")),
        gateway_url=configured.github_gateway_url, python=GRAPH_PYTHON, runner=GRAPH_RUNNER,
        cache_bytes=int(os.environ.get("REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES", str(10 * 1024**3))),
    )
    slots = threading.BoundedSemaphore(4)

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(40)

        def log_message(self, format: str, *args: object) -> None:
            pass

        def write(self, status: int, value: dict[str, object]) -> None:
            raw = json.dumps(value, separators=(",", ":")).encode()
            if len(raw) > GRAPH_RESULT_MAX_BYTES:
                status, raw = 503, b'{"status":"unavailable"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.write(200, {"status": "ok"})
            else:
                self.write(404, {"status": "not_found"})

        def do_POST(self) -> None:
            if self.path != GRAPH_QUERY_PATH:
                self.write(404, {"status": "not_found"})
                return
            if not slots.acquire(blocking=False):
                self.write(503, {"status": "busy"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 1 <= length <= 4096:
                    raise GraphError("invalid graph request size")
                decoded: object = json.loads(self.rfile.read(length))
                if not isinstance(decoded, dict):
                    raise GraphError("invalid graph request")
                result = service.handle(cast(dict[str, object], decoded))
                self.write(200, result)
            except (ValueError, UnicodeError):
                self.write(400, {"status": "unavailable"})
            except GitHubGatewayError:
                self.write(409, {"status": "unavailable"})
            except Exception as exc:
                logging.getLogger(__name__).warning("Code graph request unavailable: %s", type(exc).__name__)
                self.write(503, {"status": "unavailable"})
            finally:
                slots.release()

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server((host, port), Handler)

    def stop(_signum: int, _frame: object) -> None:
        service.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        service.close()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indexer", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8647)
    args = parser.parse_args()
    if args.indexer:
        from ..code_graph.indexer import main as index
        return index()
    logging.basicConfig(level=logging.INFO)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
