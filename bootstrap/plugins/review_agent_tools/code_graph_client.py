"""Short, bounded calls to the optional graph service."""

from __future__ import annotations

import json
from http.client import HTTPException
from typing import Literal, cast
import urllib.error
import urllib.request

from .code_graph_contract import GRAPH_QUERY_PATH, GRAPH_RESULT_MAX_BYTES, GraphError, GraphIdentity
from .source_control import SameOriginHttpsRedirectHandler


def request_graph(
    url: str, identity: GraphIdentity, *, operation: Literal["prepare", "query"],
    pattern: str = "", target: str = "",
) -> dict[str, object]:
    request = urllib.request.Request(
        url + GRAPH_QUERY_PATH,
        data=json.dumps({"identity": identity.to_mapping(), "operation": operation,
                         "pattern": pattern, "target": target}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(SameOriginHttpsRedirectHandler())
        with opener.open(request, timeout=3 if operation == "prepare" else 35) as response:
            raw = response.read(GRAPH_RESULT_MAX_BYTES + 1)
        if len(raw) > GRAPH_RESULT_MAX_BYTES:
            raise GraphError("graph response exceeds its bound")
        decoded: object = json.loads(raw)
        if not isinstance(decoded, dict):
            raise GraphError("invalid graph response")
        result = cast(dict[str, object], decoded)
        status = result.get("status")
        if not isinstance(status, str) or status not in {"disabled", "building", "queued", "busy", "ready", "unavailable", "embeddings_unavailable"}:
            raise GraphError("invalid graph status")
        return result
    except urllib.error.HTTPError as exc:
        exc.close()
        raise GraphError("graph service is unavailable") from None
    except (HTTPException, OSError, ValueError):
        raise GraphError("graph service is unavailable") from None
