from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"))

from review_agent_tools.code_graph_contract import (  # noqa: E402
    EMBEDDING_DIMENSIONS, GraphError, GraphIdentity, GraphPolicy, GraphSubject,
)
from review_agent_tools.code_graph_cache import extract_snapshot  # noqa: E402
from review_agent_tools.code_graph_embeddings import embedding_inputs, openai_embeddings  # noqa: E402
from review_agent_tools.code_graph_service import CodeGraphService  # noqa: E402
from review_agent_tools import code_graph_indexer  # noqa: E402


_RUNNER = '''import json, pathlib, sys
request = json.load(sys.stdin)
database = pathlib.Path(request["database"])
source = pathlib.Path(request["source"])
if request["operation"] == "build":
    values = json.loads(database.read_text()) if database.exists() else {}
    changed = request["changed_files"]
    paths = changed if changed is not None else [str(p.relative_to(source)) for p in source.rglob("*.py")]
    for name in paths:
        path = source / name
        if path.exists():
            values[name] = path.read_text()
        else:
            values.pop(name, None)
    database.write_text(json.dumps(values))
    state = "unavailable" if request["embeddings"] == "openai" else "disabled"
    print(json.dumps({"embedding_state": state, "parse_errors": 0, "nodes": len(values)}))
elif request["operation"] == "embed":
    print(json.dumps({"embedding_state": "unavailable", "embedded": 0}))
else:
    values = json.loads(database.read_text())
    print(json.dumps({"results": [{"path": path, "symbol": content} for path, content in values.items()], "search_mode": "keyword"}))
'''


def archive(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        for name, data, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(data)
            entry.linkname = "../../outside"
            stream.addfile(entry, io.BytesIO(data))
    return output.getvalue()


class GraphContractTests(unittest.TestCase):
    def test_structural_queries_distinguish_ambiguous_and_missing_symbols(self) -> None:
        graph, query = Mock(), Mock()
        row = {"file_path": "/snapshot/validation.py", "line_start": 3, "line_end": 5,
               "qualified_name": "/snapshot/validation.py::validate", "kind": "Function"}
        with (
            patch.object(code_graph_indexer.metadata, "version", return_value="2.3.8"),
            patch.object(code_graph_indexer, "import_module", side_effect=lambda name: {
                "code_review_graph.graph": graph, "code_review_graph.tools.query": query,
            }[name]),
        ):
            request = {"operation": "query", "source": "/snapshot", "database": "/index/graph.db",
                       "pattern": "callers_of", "target": "validate"}
            query.query_graph.return_value = {"status": "ambiguous", "candidates": [row]}
            ambiguous = code_graph_indexer.execute(request)
            self.assertEqual(ambiguous["resolution"], "ambiguous")
            self.assertEqual(ambiguous["candidates"][0]["symbol"], "validation.py::validate")
            query.query_graph.return_value = {"status": "not_found"}
            self.assertEqual(code_graph_indexer.execute(request)["resolution"], "not_found")
            query.query_graph.return_value = {"status": "ok", "results": [{**row, "target_resolution": "unresolved"}]}
            resolved = code_graph_indexer.execute(request)
            self.assertEqual(resolved["resolution"], "ok")
            self.assertEqual(resolved["results"][0]["target_resolution"], "unresolved")

    def test_graph_and_cloud_access_require_explicit_configuration(self) -> None:
        self.assertFalse(GraphPolicy.from_environment({}).enabled)
        self.assertFalse(GraphPolicy.from_environment({
            "REVIEW_AGENT_CODE_GRAPH_ENABLED": "false",
            "REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS": "openai",
        }).enabled)
        policy = GraphPolicy.from_environment({
            "REVIEW_AGENT_CODE_GRAPH_ENABLED": "true",
        })
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.embeddings, "none")
        configured = GraphPolicy.from_environment({
            "REVIEW_AGENT_CODE_GRAPH_ENABLED": "true",
            "REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS": "openai",
            "REVIEW_AGENT_OPENAI_API_KEY": "synthetic-test-key",
        })
        self.assertEqual(configured.openai_api_key, "synthetic-test-key")
        self.assertNotIn("synthetic-test-key", repr(configured))
        with self.assertRaises(GraphError):
            GraphPolicy.from_environment({
                "REVIEW_AGENT_CODE_GRAPH_ENABLED": "true",
                "REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS": "openai",
            })
        with self.assertRaises(GraphError):
            GraphPolicy.from_environment({"REVIEW_AGENT_CODE_GRAPH_ENABLED": "treu"})

    def test_graph_identity_cannot_supply_repository_or_revision(self) -> None:
        values = {"run_id": 1, "job_id": 2, "lease_generation": 3}
        self.assertEqual(GraphIdentity.from_mapping(values).to_mapping(), values)
        for invalid in ({**values, "repository": "other/repo"}, {**values, "job_id": True}):
            with self.assertRaises(GraphError):
                GraphIdentity.from_mapping(invalid)

    def test_archive_extracts_regular_source_without_repository_parser_configuration(self) -> None:
        raw = archive([
            ("snapshot/package/code.py", b"def example(): pass\n", tarfile.REGTYPE),
            ("snapshot/.code-review-graph/languages.toml", b"untrusted", tarfile.REGTYPE),
            ("snapshot/link", b"", tarfile.SYMTYPE),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = extract_snapshot(raw, root)
            self.assertEqual(set(result.files), {"package/code.py"})
            self.assertEqual(result.skipped_files, 2)
            self.assertEqual((root / "package/code.py").read_bytes(), b"def example(): pass\n")
            self.assertFalse((root / ".code-review-graph").exists())
            self.assertFalse((root / "link").exists())

    def test_archive_rejects_traversal_links_duplicates_and_expansion_limit(self) -> None:
        cases = [
            [("snapshot/../../outside", b"x", tarfile.REGTYPE)],
            [("snapshot/link", b"", tarfile.SYMTYPE)],
            [("snapshot/link", b"", tarfile.LNKTYPE)],
            [("snapshot/device", b"", tarfile.CHRTYPE)],
            [("snapshot/a", b"a", tarfile.REGTYPE), ("snapshot/a", b"b", tarfile.REGTYPE)],
            [("snapshot/a", b"0123456789", tarfile.REGTYPE)],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(GraphError):
                    extract_snapshot(archive(entries), Path(temporary), max_bytes=8)

    def test_embedding_boundary_returns_ordered_vectors_and_hides_provider_errors(self) -> None:
        inputs = embedding_inputs({"model": "text-embedding-3-small", "input": ["a", "b"]})
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"data": [
            {"index": 1, "embedding": [0.5] * EMBEDDING_DIMENSIONS},
            {"index": 0, "embedding": [0.25] * EMBEDDING_DIMENSIONS},
        ]}).encode())
        result = openai_embeddings(inputs, "synthetic-test-key", opener=opener)
        self.assertEqual(result["data"][0]["embedding"][0], 0.25)
        self.assertNotIn("synthetic-test-key", json.dumps(result))
        opener.open.return_value = io.BytesIO(b'{"error":"synthetic-test-key"}')
        with self.assertRaises(GraphError) as raised:
            openai_embeddings(inputs, "synthetic-test-key", opener=opener)
        self.assertNotIn("synthetic-test-key", str(raised.exception))
        with self.assertRaises(GraphError):
            embedding_inputs({"model": "another-model", "input": ["a"]})


class GraphCacheTests(unittest.TestCase):
    def test_exact_commits_and_repositories_remain_isolated_through_incremental_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(_RUNNER)
            service = CodeGraphService(
                root=root / "cache", gateway_url="http://gateway:8646",
                python=Path(sys.executable), runner=runner,
            )
            client = Mock()
            service.client = client
            identity = GraphIdentity(1, 2, 3).to_mapping()
            request = {"identity": identity, "operation": "prepare", "pattern": "", "target": ""}
            query = {**request, "operation": "query", "pattern": "symbol", "target": "example"}

            def build(subject: GraphSubject, content: str) -> dict[str, object]:
                client.get_code_graph_subject.return_value = subject
                client.get_code_graph_archive.return_value = archive([
                    ("snapshot/example.py", content.encode(), tarfile.REGTYPE),
                ])
                self.assertEqual(service.handle(request)["status"], "building")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = service.handle(query)
                    if result["status"] == "ready":
                        return result
                    time.sleep(0.01)
                self.fail("graph preparation did not produce a ready snapshot")

            first = GraphSubject(91, "example/one", "a" * 40, True, "none")
            second = GraphSubject(91, "example/one", "b" * 40, True, "none")
            another = GraphSubject(92, "example/two", "a" * 40, True, "none")
            try:
                self.assertEqual(build(first, "first")["results"][0]["symbol"], "first")
                self.assertEqual(build(second, "second")["results"][0]["symbol"], "second")
                self.assertEqual(build(another, "other repository")["results"][0]["symbol"], "other repository")
                client.get_code_graph_subject.return_value = first
                self.assertEqual(service.handle(request)["status"], "ready")
                self.assertEqual(service.handle(query)["results"][0]["symbol"], "first")
                self.assertEqual(client.get_code_graph_archive.call_count, 3)
                cloud = GraphSubject(91, "example/one", "a" * 40, True, "openai")
                self.assertEqual(build(cloud, "first")["embedding_state"], "unavailable")
                request["identity"] = GraphIdentity(2, 2, 3).to_mapping()
                self.assertEqual(service.handle(request)["status"], "building")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and service.handle(request)["status"] == "building":
                    time.sleep(0.01)
                self.assertEqual(service.handle(request)["status"], "ready")
                self.assertEqual(client.get_code_graph_archive.call_count, 4)
                client.get_code_graph_subject.side_effect = [first, another]
                with self.assertRaises(GraphError):
                    service.handle(query)
            finally:
                service.close()

    def test_failed_build_does_not_publish_an_index_or_retry_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(_RUNNER)
            service = CodeGraphService(root=root / "cache", gateway_url="http://gateway:8646",
                                       python=Path(sys.executable), runner=runner)
            client = Mock()
            client.get_code_graph_subject.return_value = GraphSubject(91, "example/one", "a" * 40, True, "none")
            client.get_code_graph_archive.return_value = b"invalid archive"
            service.client = client
            request = {"identity": GraphIdentity(1, 2, 3).to_mapping(), "operation": "prepare", "pattern": "", "target": ""}
            try:
                with patch("review_agent_tools.code_graph_service.threading.Thread.start", side_effect=RuntimeError("thread limit")):
                    self.assertEqual(service.handle(request)["status"], "unavailable")
                request["identity"] = GraphIdentity(2, 2, 3).to_mapping()
                self.assertEqual(service.handle(request)["status"], "building")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and service.handle(request)["status"] == "building":
                    time.sleep(0.01)
                self.assertEqual(service.handle(request)["status"], "unavailable")
                self.assertEqual(client.get_code_graph_archive.call_count, 1)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
