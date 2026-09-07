"""Optional graph lifecycle, isolated from the GitHub and embedding credentials."""

from __future__ import annotations

import base64
from collections import OrderedDict, deque
from collections.abc import Mapping
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import cast

from .code_graph_cache import extract_snapshot
from .code_graph_contract import (
    EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, GRAPH_MAX_BYTES, GRAPH_RESULT_MAX_BYTES,
    GraphError, GraphIdentity, GraphSubject, QUERY_PATTERNS, SNAPSHOT_MAX_BYTES,
)
from .github.gateway_client import ReviewGitHubGatewayClient

_MANIFEST_MAX_BYTES = 16_000_000
_PREPARATION_QUEUE_LIMIT = 4
logger = logging.getLogger(__name__)


class CodeGraphService:
    """Serialize disposable index work and serve only complete exact-commit graphs."""

    def __init__(
        self, *, root: Path, gateway_url: str, python: Path, runner: Path,
        cache_bytes: int = 10 * 1024**3,
    ) -> None:
        if not root.is_absolute() or not python.is_absolute() or not runner.is_absolute():
            raise GraphError("graph runtime paths must be absolute")
        if cache_bytes < GRAPH_MAX_BYTES:
            raise GraphError("graph cache budget must allow one maximum-size graph")
        root = root.resolve()
        self.root = root
        self.cache = root / "snapshots"
        self.work = root / "work"
        self.source = self.work / "source"
        self.python = python
        self.runner = runner
        self.gateway_url = gateway_url
        self.client = ReviewGitHubGatewayClient(gateway_url)
        self.cache_bytes = cache_bytes
        self._slot = threading.Lock()
        self._state_lock = threading.Lock()
        self._active: str | None = None
        self._active_identity: GraphIdentity | None = None
        self._thread: threading.Thread | None = None
        self._pending: OrderedDict[str, tuple[GraphIdentity, GraphSubject]] = OrderedDict()
        self._failed: deque[tuple[int, str]] = deque(maxlen=_PREPARATION_QUEUE_LIMIT + 1)
        self._process: subprocess.Popen[bytes] | None = None
        self._stopping = False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache.mkdir(exist_ok=True)
        # This directory contains only interrupted disposable work, never saved snapshots.
        if self.work.exists():
            shutil.rmtree(self.work)
        (self.source / ".code-review-graph").mkdir(parents=True)

    def _key(self, subject: GraphSubject) -> str:
        # CRG's node IDs include absolute source paths and vector identity includes
        # the proxy origin. A moved workspace or gateway needs compatible new state.
        configuration = hashlib.sha256(f"{self.source}:{self.gateway_url}".encode()).hexdigest()
        return f"{subject.repository_id}/{subject.cache_identity}-{configuration}/{subject.head_sha}"

    def _manifest(self, subject: GraphSubject) -> dict[str, object] | None:
        directory = self.cache / self._key(subject)
        try:
            with (directory / "manifest.json").open("rb") as stream:
                raw = stream.read(_MANIFEST_MAX_BYTES + 1)
            if len(raw) > _MANIFEST_MAX_BYTES or not (directory / "graph.db").is_file():
                return None
            decoded: object = json.loads(raw)
            if not isinstance(decoded, dict):
                return None
            data = cast(dict[str, object], decoded)
            if data.get("head_sha") != subject.head_sha or data.get("cache_identity") != subject.cache_identity:
                return None
            if data.get("repository_id") != subject.repository_id:
                return None
            return data
        except (OSError, ValueError):
            return None

    @staticmethod
    def _status(subject: GraphSubject, status: str, manifest: Mapping[str, object] | None = None) -> dict[str, object]:
        return {
            "status": status, "repository_id": subject.repository_id, "head_sha": subject.head_sha,
            "embedding_state": (manifest or {}).get("embedding_state", "pending" if subject.embeddings == "openai" else "disabled"),
            "skipped_files": (manifest or {}).get("skipped_files", 0),
            "parse_errors": (manifest or {}).get("parse_errors", 0),
        }

    def handle(self, request: Mapping[str, object]) -> dict[str, object]:
        if set(request) != {"identity", "operation", "pattern", "target"}:
            raise GraphError("invalid graph request fields")
        raw_identity = request.get("identity")
        if not isinstance(raw_identity, dict):
            raise GraphError("invalid graph request identity")
        identity = GraphIdentity.from_mapping(cast(dict[str, object], raw_identity))
        operation, pattern, target = request.get("operation"), request.get("pattern"), request.get("target")
        if not isinstance(operation, str) or operation not in {"prepare", "query"} or not isinstance(pattern, str) or not isinstance(target, str):
            raise GraphError("invalid graph operation")
        if len(target) > 512 or (operation == "query" and (pattern not in QUERY_PATTERNS or not target.strip())):
            raise GraphError("invalid graph query")
        subject = self.client.get_code_graph_subject(identity)
        if not subject.enabled:
            return self._status(subject, "disabled")
        if operation == "prepare":
            return self._prepare(identity, subject)
        if not self._slot.acquire(blocking=False):
            with self._state_lock:
                key = self._key(subject)
                status = "building" if self._active == key else "queued" if key in self._pending else "busy"
                return self._status(subject, status)
        try:
            manifest = self._manifest(subject)
            if manifest is None:
                return self._status(subject, "unavailable")
            if pattern == "semantic" and (subject.embeddings != "openai" or manifest.get("embedding_state") != "ready"):
                return self._status(subject, "embeddings_unavailable", manifest)
            directory = self.cache / self._key(subject)
            response = self._execute(identity, subject, {
                "operation": "query", "database": str(directory / "graph.db"),
                "pattern": pattern, "target": target,
            }, timeout=30)
            if self.client.get_code_graph_subject(identity) != subject:
                raise GraphError("graph authority changed")
            os.utime(directory, None)
            return {**response, **self._status(subject, "ready", manifest)}
        finally:
            with self._state_lock:
                self._slot.release()
                self._start_next()

    def _prepare(self, identity: GraphIdentity, subject: GraphSubject) -> dict[str, object]:
        key = self._key(subject)
        with self._state_lock:
            if self._stopping:
                return self._status(subject, "unavailable")
            if self._active == key and self._active_identity == identity:
                return self._status(subject, "building")
            manifest = self._manifest(subject)
            if manifest is not None and (
                subject.embeddings == "none" or manifest.get("embedding_state") == "ready"
                or manifest.get("embedding_attempt_run_id") == identity.run_id
            ):
                return self._status(subject, "ready", manifest)
            if (identity.run_id, key) in self._failed:
                return self._status(subject, "unavailable")
            if key not in self._pending and len(self._pending) >= _PREPARATION_QUEUE_LIMIT:
                return self._status(subject, "busy")
            self._pending[key] = identity, subject
            self._start_next()
            status = "building" if self._active == key else "queued" if key in self._pending else "unavailable"
            return self._status(subject, status)

    def _start_next(self) -> None:
        # Called with the state lock held after enqueueing or releasing the one
        # process slot. Pending work carries no authority beyond its original lease.
        while self._pending and not self._stopping:
            if not self._slot.acquire(blocking=False):
                return
            key, (identity, subject) = self._pending.popitem(last=False)
            self._active = key
            self._active_identity = identity
            thread = threading.Thread(target=self._build, args=(identity, subject), daemon=True)
            try:
                thread.start()
                self._thread = thread
                return
            except RuntimeError:
                self._active = None
                self._active_identity = None
                self._failed.append((identity.run_id, key))
                self._slot.release()

    def _evict(self, protected: Path) -> None:
        snapshots: list[tuple[Path, int]] = []
        for directory in self.cache.glob("*/*/*"):
            if not directory.is_dir():
                continue
            if not (directory / "manifest.json").is_file() or not (directory / "graph.db").is_file():
                shutil.rmtree(directory)
                continue
            size = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
            snapshots.append((directory, size))
        total = sum(size for _, size in snapshots)
        for directory, size in sorted(snapshots, key=lambda item: item[0].stat().st_mtime):
            if total <= self.cache_bytes - GRAPH_MAX_BYTES:
                break
            if directory == protected:
                continue
            shutil.rmtree(directory)
            total -= size
        if shutil.disk_usage(self.root).free < 2 * GRAPH_MAX_BYTES + SNAPSHOT_MAX_BYTES:
            raise GraphError("graph workspace has insufficient free space")

    def _build(self, identity: GraphIdentity, subject: GraphSubject) -> None:
        started = time.monotonic()
        try:
            if self.client.get_code_graph_subject(identity) != subject:
                raise GraphError("graph authority changed")
            directory = self.cache / self._key(subject)
            self._evict(directory)
            manifest = self._manifest(subject)
            if manifest is not None and (
                subject.embeddings == "none" or manifest.get("embedding_state") == "ready"
                or manifest.get("embedding_attempt_run_id") == identity.run_id
            ):
                return
            if manifest is None:
                stage = self.work / "index"
                stage.mkdir(exist_ok=True)
                previous = sorted(directory.parent.glob("*/manifest.json"), key=lambda path: path.parent.stat().st_mtime, reverse=True)
                old_files: dict[str, str] | None = None
                if previous:
                    candidate = GraphSubject(subject.repository_id, subject.repository, previous[0].parent.name, True, subject.embeddings)
                    prior = self._manifest(candidate)
                    if prior is not None and isinstance(prior.get("files"), dict):
                        raw_files = cast(dict[str, object], prior["files"])
                        if all(isinstance(value, str) for value in raw_files.values()):
                            old_files = cast(dict[str, str], raw_files)
                            # SQLite backup includes committed WAL batches left by
                            # an interrupted embedding process on the previous head.
                            with (
                                closing(sqlite3.connect((previous[0].parent / "graph.db").as_uri() + "?mode=ro", uri=True)) as saved,
                                closing(sqlite3.connect(stage / "graph.db")) as staged,
                            ):
                                saved.backup(staged)
                if self.source.exists():
                    shutil.rmtree(self.source)
                self.source.mkdir()
                snapshot = extract_snapshot(self.client.get_code_graph_archive(identity), self.source)
                changed = None if old_files is None else sorted(
                    name for name in old_files.keys() | snapshot.files.keys()
                    if old_files.get(name) != snapshot.files.get(name)
                )
                result = self._execute(identity, subject, {
                    "operation": "build", "changed_files": changed,
                    "database": str(stage / "graph.db"),
                }, timeout=max(1, 300 - (time.monotonic() - started)))
                if (stage / "graph.db").stat().st_size > GRAPH_MAX_BYTES:
                    raise GraphError("graph exceeds its size limit")
                manifest = {
                    "files": snapshot.files, "skipped_files": snapshot.skipped_files, **result,
                    "repository_id": subject.repository_id, "head_sha": subject.head_sha,
                    "cache_identity": subject.cache_identity,
                }
                directory.mkdir(parents=True, exist_ok=True)
                os.replace(stage / "graph.db", directory / "graph.db")
                self._save_manifest(directory, manifest)
            if subject.embeddings == "openai":
                # Keep the authorized snapshot and committed vector batches even
                # if the review ends. Every external request and later query is
                # still authorized by the gateway; saving local data grants no access.
                manifest = {**manifest, "embedding_state": "unavailable",
                            "embedding_attempt_run_id": identity.run_id}
                self._save_manifest(directory, manifest)
                if self.client.get_code_graph_subject(identity) != subject:
                    raise GraphError("graph authority changed")
                result = self._execute(identity, subject, {
                    "operation": "embed", "database": str(directory / "graph.db"),
                }, timeout=max(1, 300 - (time.monotonic() - started)))
                self._save_manifest(directory, {**manifest, **result})
            os.utime(directory, None)
        except Exception as exc:
            logger.warning(
                "Code graph preparation unavailable: repository_id=%d head_sha=%s error_type=%s",
                subject.repository_id, subject.head_sha, type(exc).__name__,
            )
            with self._state_lock:
                self._failed.append((identity.run_id, self._key(subject)))
        finally:
            # The source root remains for CRG's path validation; queries read saved graph rows.
            try:
                shutil.rmtree(self.work, ignore_errors=True)
                (self.source / ".code-review-graph").mkdir(parents=True, exist_ok=True)
            finally:
                with self._state_lock:
                    self._active = None
                    self._active_identity = None
                    self._slot.release()
                    self._start_next()

    @staticmethod
    def _save_manifest(directory: Path, manifest: Mapping[str, object]) -> None:
        pending = directory / "manifest.json.tmp"
        pending.write_text(json.dumps(manifest), encoding="utf-8")
        os.replace(pending, directory / "manifest.json")

    def close(self) -> None:
        with self._state_lock:
            self._stopping = True
            self._pending.clear()
            thread = self._thread
        process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if thread is not None:
            thread.join(timeout=5)

    def _execute(
        self, identity: GraphIdentity, subject: GraphSubject, request: dict[str, object], *, timeout: float,
    ) -> dict[str, object]:
        if self._stopping:
            raise GraphError("graph service is stopping")
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8", "CRG_SERIAL_PARSE": "1", "CRG_PARSE_WORKERS": "1",
            "CRG_ALLOW_REMOTE_CODE": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        }
        embeddings = "none" if request["operation"] == "build" else subject.embeddings
        if embeddings == "openai":
            environment.update({
                "CRG_OPENAI_API_KEY": base64.b64encode(json.dumps(identity.to_mapping()).encode()).decode("ascii"),
                "CRG_OPENAI_BASE_URL": self.gateway_url.rstrip("/") + "/v1/code-graph",
                "CRG_OPENAI_MODEL": EMBEDDING_MODEL,
                "CRG_OPENAI_DIMENSION": str(EMBEDDING_DIMENSIONS),
                "CRG_OPENAI_BATCH_SIZE": "64",
            })
        payload = json.dumps({**request, "source": str(self.source), "embeddings": embeddings}).encode()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [str(self.python), str(self.runner), "--indexer"],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env=environment, start_new_session=True, cwd=self.root,
            )
            self._process = process
            try:
                if self._stopping:
                    raise GraphError("graph service is stopping")
                process.communicate(payload, timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            finally:
                self._process = None
            output.seek(0)
            raw = output.read(GRAPH_RESULT_MAX_BYTES + 1)
        if process.returncode != 0 or len(raw) > GRAPH_RESULT_MAX_BYTES:
            raise GraphError("graph process is unavailable")
        decoded: object = json.loads(raw)
        if not isinstance(decoded, dict):
            raise GraphError("invalid graph process response")
        return cast(dict[str, object], decoded)
