"""Narrow adapter to the pinned CRG runtime, executed in the isolated graph service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import resource
import sqlite3
import sys
from typing import Protocol, cast

from .code_graph_contract import (
    EMBEDDING_MODEL, GRAPH_MAX_BYTES, GRAPH_MAX_NODES, GRAPH_RESULT_MAX_BYTES, GraphError,
)


class _Store(Protocol):
    def close(self) -> None: ...
    def get_all_nodes(self, exclude_files: bool = True) -> list[object]: ...
    def search_nodes(self, query: str, limit: int = 20) -> list[object]: ...


class _GraphModule(Protocol):
    def GraphStore(self, db_path: Path) -> _Store: ...
    def node_to_dict(self, node: object) -> dict[str, object]: ...


class _IncrementalModule(Protocol):
    def full_build(self, repo_root: Path, store: _Store, recurse_submodules: bool = False) -> dict[str, object]: ...
    def incremental_update(self, repo_root: Path, store: _Store, *, changed_files: list[str]) -> dict[str, object]: ...


class _VectorStore(Protocol):
    def embed_nodes(self, nodes: list[object], batch_size: int = 64) -> int: ...
    def purge_orphans(self) -> int: ...
    def close(self) -> None: ...


class _EmbeddingModule(Protocol):
    def EmbeddingStore(self, db_path: Path, provider: str, model: str) -> _VectorStore: ...


class _QueryModule(Protocol):
    def query_graph(self, pattern: str, target: str, *, repo_root: str, max_results: int) -> dict[str, object]: ...


class _SearchModule(Protocol):
    def hybrid_search(self, store: _Store, query: str, *, limit: int, provider: str, model: str,
                      _out_mode: list[str]) -> list[dict[str, object]]: ...


def _locations(rows: list[object], root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in rows[:10]:
        if not isinstance(raw, dict):
            continue
        row = cast(dict[str, object], raw)
        path, start, end = row.get("file_path"), row.get("line_start"), row.get("line_end")
        if not isinstance(path, str) or type(start) is not int or type(end) is not int or start < 1 or end < start:
            continue
        try:
            relative = Path(path).relative_to(root).as_posix()
        except ValueError:
            continue
        if len(relative) > 1024 or ".." in Path(relative).parts:
            continue
        qualified = row.get("qualified_name", "")
        symbol = str(qualified).removeprefix(str(root) + "/")[:1500]
        item: dict[str, object] = {
            "path": relative, "line_start": start, "line_end": end,
            "symbol": symbol, "kind": str(row.get("kind", ""))[:40],
        }
        if row.get("target_resolution") == "unresolved":
            item["target_resolution"] = "unresolved"
        result.append(item)
    return result


def execute(request: Mapping[str, object]) -> dict[str, object]:
    if metadata.version("code-review-graph") != "2.3.8":
        raise GraphError("unsupported graph runtime version")
    root = Path(str(request["source"]))
    database = Path(str(request["database"]))
    os.environ["CRG_DATA_DIR"] = str(database.parent)
    graph = cast(_GraphModule, import_module("code_review_graph.graph"))
    store = graph.GraphStore(database)
    try:
        build_result: dict[str, object] = {}
        if request["operation"] == "build":
            incremental = cast(_IncrementalModule, import_module("code_review_graph.incremental"))
            changed = request.get("changed_files")
            if isinstance(changed, list):
                build = incremental.incremental_update(root, store, changed_files=cast(list[str], changed))
            else:
                build = incremental.full_build(root, store, recurse_submodules=False)
            postprocess = cast(
                Callable[[_Store, dict[str, object], str], list[str]],
                getattr(import_module("code_review_graph.tools.build"), "_run_postprocess"),
            )
            warnings = postprocess(store, build, "minimal")
            with sqlite3.connect(database) as connection:
                nodes = int(connection.execute("SELECT count(*) FROM nodes").fetchone()[0])
            if nodes > GRAPH_MAX_NODES or database.stat().st_size > GRAPH_MAX_BYTES:
                raise GraphError("graph exceeds its size limit")
            errors = build.get("errors", [])
            build_result = {"nodes": nodes, "postprocess_warnings": len(warnings),
                            "parse_errors": len(cast(list[object], errors)) if isinstance(errors, list) else 0}
        if request["operation"] in {"build", "embed"}:
            embedded = 0
            embedding_state = "disabled"
            if request.get("embeddings") == "openai":
                embeddings = cast(_EmbeddingModule, import_module("code_review_graph.embeddings"))
                vectors = embeddings.EmbeddingStore(database, provider="openai", model=EMBEDDING_MODEL)
                try:
                    vectors.purge_orphans()
                    embedded = vectors.embed_nodes(store.get_all_nodes())
                    embedding_state = "ready"
                except Exception:
                    embedding_state = "unavailable"
                finally:
                    vectors.close()
            return {**build_result, "embedded": embedded, "embedding_state": embedding_state}
        pattern, target = str(request["pattern"]), str(request["target"])
        resolution: dict[str, object] = {}
        if pattern == "symbol":
            rows: list[object] = [graph.node_to_dict(node) for node in store.search_nodes(target, limit=10)]
            mode = "keyword"
        elif pattern == "semantic":
            search = cast(_SearchModule, import_module("code_review_graph.search"))
            modes: list[str] = []
            rows = list(search.hybrid_search(
                store, target, limit=10, provider="openai", model=EMBEDDING_MODEL, _out_mode=modes,
            ))
            mode = modes[0] if modes else "keyword"
        else:
            query = cast(_QueryModule, import_module("code_review_graph.tools.query"))
            response = query.query_graph(pattern, target, repo_root=str(root), max_results=10)
            status = response.get("status")
            if status not in {"ok", "ambiguous", "not_found"}:
                raise GraphError("graph query is unavailable")
            resolution["resolution"] = status
            if status == "ambiguous":
                candidates = response.get("candidates", [])
                resolution["candidates"] = _locations(cast(list[object], candidates), root) if isinstance(candidates, list) else []
            raw_rows = response.get("results", [])
            rows = cast(list[object], raw_rows) if isinstance(raw_rows, list) else []
            mode = "structural"
        return {"results": _locations(rows, root), "search_mode": mode, **resolution}
    finally:
        store.close()


def main() -> int:
    try:
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (GRAPH_MAX_BYTES, GRAPH_MAX_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (300, 305))
        decoded: object = json.loads(sys.stdin.buffer.read(16_000_001))
        if not isinstance(decoded, dict):
            raise GraphError("invalid graph process request")
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            result = execute(cast(dict[str, object], decoded))
        rendered = json.dumps(result, separators=(",", ":"))
        if len(rendered.encode("utf-8")) > GRAPH_RESULT_MAX_BYTES:
            raise GraphError("graph response exceeds its bound")
        print(rendered)
        return 0
    except Exception:
        print('{"status":"unavailable"}')
        return 1
