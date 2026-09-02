#!/usr/bin/env python3
"""Measure the bounded Review Agent paths used by the v0.1 scale claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Lock
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import admission, changed_files  # noqa: E402
from review_agent_tools.postgres import (  # noqa: E402
    jobs,
    publications,
)
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


def _milliseconds(start: int) -> float:
    return round((time.perf_counter_ns() - start) / 1_000_000, 2)


def _peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 2)


def _database_io(database_url: str) -> tuple[int, int]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT blks_read, blks_hit
            FROM pg_stat_database
            WHERE datname = current_database()
            """
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _io_delta(before: tuple[int, int], after: tuple[int, int]) -> dict[str, int]:
    return {
        "buffer_reads": max(0, after[0] - before[0]),
        "buffer_hits": max(0, after[1] - before[1]),
    }


def _changed_files() -> dict[str, object]:
    files = [
        {
            "filename": f"src/file_{index:04d}.py",
            "status": "modified",
            "sha": f"{index:040x}",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }
        for index in range(3_000)
    ]
    requests = 0

    def request(per_page: int, page: int) -> tuple[bytes, bool, dict[str, str]]:
        nonlocal requests
        requests += 1
        start = (page - 1) * per_page
        body = json.dumps(files[start : start + per_page]).encode()
        return body, False, {}

    tracemalloc.start()
    cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    result = changed_files.enumerate_changed_files(request, reported=len(files))
    wall_ms = _milliseconds(wall_start)
    cpu_ms = round((time.process_time_ns() - cpu_start) / 1_000_000, 2)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "files": result.registered,
        "index_state": result.index_state,
        "requests": requests,
        "wall_ms": wall_ms,
        "cpu_ms": cpu_ms,
        "python_peak_mib": round(peak / 1024 / 1024, 2),
        "process_peak_rss_mib": _peak_rss_mib(),
    }


def _reset_and_seed(database_url: str, count: int) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
    with psycopg.connect(database_url) as connection:
        runner.apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO review_agent.repositories (
                provider, provider_repository_id, owner, name, full_name,
                created_at, updated_at
            )
            SELECT 'github', value, 'benchmark', 'repo-' || value,
                   'benchmark/repo-' || value,
                   statement_timestamp(), statement_timestamp()
            FROM generate_series(1, %s) AS value
            """,
            (count,),
        )
        connection.execute(
            """
            INSERT INTO review_agent.pull_requests (
                repository_id, number, created_at
            )
            SELECT id, 1, statement_timestamp()
            FROM review_agent.repositories
            """
        )
        connection.execute(
            """
            INSERT INTO review_agent.review_subjects (
                pull_request_id, base_sha, head_sha, policy_revision,
                resolved_config_schema_version, resolved_config,
                resolved_config_hash, created_at
            )
            SELECT id, repeat('b', 40), lpad(to_hex(id), 40, '0'),
                   'benchmark-v1', 1, '{}'::jsonb, repeat('c', 64),
                   statement_timestamp()
            FROM review_agent.pull_requests
            """
        )
        connection.execute(
            """
            INSERT INTO review_agent.review_runs (
                pull_request_id, review_subject_id, request_key, status, phase,
                started_at, last_heartbeat_at
            )
            SELECT pull_request.id, subject.id, 'benchmark:' || pull_request.id,
                   'running', 'accepted', statement_timestamp(),
                   statement_timestamp()
            FROM review_agent.pull_requests AS pull_request
            JOIN review_agent.review_subjects AS subject
              ON subject.pull_request_id = pull_request.id
            """
        )
        connection.execute(
            """
            INSERT INTO review_agent.review_jobs (
                review_run_id, status, priority, available_at, attempt_count,
                max_attempts, lease_generation, created_at
            )
            SELECT id, 'queued', 0, statement_timestamp(), 0, 3, 0,
                   statement_timestamp()
            FROM review_agent.review_runs
            """
        )


def _claim_job(
    connection: psycopg.Connection[tuple[object, ...]], owner: str
) -> tuple[int | None, int, tuple[str, ...]]:
    claimed = jobs.claim_next_job(
        connection,
        lease_owner=owner,
        lease_duration=timedelta(minutes=2),
        priority_aging_interval=timedelta(minutes=15),
    )
    lock_row = connection.execute(
        """
        SELECT count(*), array_agg(DISTINCT mode ORDER BY mode)
        FROM pg_locks
        WHERE pid = pg_backend_pid()
        """
    ).fetchone()
    assert lock_row is not None
    modes = tuple(str(mode) for mode in (lock_row[1] or ()))
    return (
        claimed.id if claimed is not None else None,
        int(lock_row[0]),
        modes,
    )


def _queue(database_url: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for ready_jobs in (100, 1_000, 10_000):
        _reset_and_seed(database_url, ready_jobs)
        before_io = _database_io(database_url)
        samples: list[float] = []
        peak_locks = 0
        lock_modes: set[str] = set()
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl(database_url),
            role=PostgreSQLRuntimeRole.WORKER,
            worker_concurrency=1,
        )
        runtime.open()
        try:
            for index in range(min(20, ready_jobs)):
                started = time.perf_counter_ns()
                with runtime.transaction() as connection:
                    job_id, locks, modes = _claim_job(
                        connection, f"sequential-{ready_jobs}-{index}"
                    )
                assert job_id is not None
                samples.append(_milliseconds(started))
                peak_locks = max(peak_locks, locks)
                lock_modes.update(modes)
            pool = runtime.pool_metrics()
        finally:
            runtime.close()
        ordered = sorted(samples)
        results.append(
            {
                "ready_jobs": ready_jobs,
                "claims": len(samples),
                "median_ms": round(statistics.median(samples), 2),
                "p95_ms": ordered[max(0, round(0.95 * len(ordered)) - 1)],
                "max_ms": ordered[-1],
                "peak_locks": peak_locks,
                "lock_modes": sorted(lock_modes),
                "pool_waiters_peak": pool.waiting_requests,
                "process_peak_rss_mib": _peak_rss_mib(),
                **_io_delta(before_io, _database_io(database_url)),
            }
        )
    return results


def _repositories(database_url: str) -> dict[str, object]:
    _reset_and_seed(database_url, 100)
    before_io = _database_io(database_url)
    runtime = PostgreSQLRuntime(
        PostgresDatabaseUrl(database_url),
        role=PostgreSQLRuntimeRole.WORKER,
        worker_concurrency=10,
    )
    runtime.open()
    observed_waiters = 0
    peak_locks = 0
    metrics_lock = Lock()

    def worker(index: int) -> list[int]:
        nonlocal observed_waiters, peak_locks
        claimed: list[int] = []
        while True:
            with runtime.transaction() as connection:
                job_id, locks, _ = _claim_job(connection, f"worker-{index}")
            with metrics_lock:
                observed_waiters = max(
                    observed_waiters, runtime.pool_metrics().waiting_requests
                )
                peak_locks = max(peak_locks, locks)
            if job_id is None:
                return claimed
            claimed.append(job_id)

    started = time.perf_counter_ns()
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            batches = tuple(executor.map(worker, range(10)))
        pool = runtime.pool_metrics()
    finally:
        runtime.close()
    identifiers = [job_id for batch in batches for job_id in batch]
    return {
        "repositories": 100,
        "workers": 10,
        "distinct_claims": len(set(identifiers)),
        "wall_ms": _milliseconds(started),
        "peak_locks": peak_locks,
        "pool_waiters_peak": max(observed_waiters, pool.waiting_requests),
        "pool_maximum_size": pool.maximum_size,
        "process_peak_rss_mib": _peak_rss_mib(),
        **_io_delta(before_io, _database_io(database_url)),
    }


def _publishers(database_url: str) -> list[dict[str, object]]:
    _reset_and_seed(database_url, 20)
    before_io = _database_io(database_url)
    markdown = "review\n"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks_schema_version,
                rendered_blocks, rendered_hash, status, generated_at
            )
            SELECT run.pull_request_id, run.id, 1,
                   'sha256:' || lpad(to_hex(run.id), 64, '0'),
                   %s, 1, '[{"kind":"header","markdown":"review"}]'::jsonb,
                   %s, 'generated', statement_timestamp()
            FROM review_agent.review_runs AS run
            """,
            (markdown, hashlib.sha256(markdown.encode()).hexdigest()),
        )

    results: list[dict[str, object]] = []
    for replicas in (1, 2, 10):
        barrier = Barrier(replicas)

        def claim(index: int) -> tuple[int, int]:
            with psycopg.connect(database_url) as connection:
                barrier.wait()
                with connection.transaction():
                    result = publications.claim_next_publication(
                        connection,
                        lease_owner=f"publisher-{replicas}-{index}",
                        lease_duration=timedelta(minutes=2),
                    )
                    lock_row = connection.execute(
                        "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"
                    ).fetchone()
            assert result is not None and result.acquired
            assert lock_row is not None
            return int(result.publication.id), int(lock_row[0])

        started = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=replicas) as executor:
            claims = tuple(executor.map(claim, range(replicas)))
        after_io = _database_io(database_url)
        results.append(
            {
                "replicas": replicas,
                "distinct_claims": len({identifier for identifier, _ in claims}),
                "wall_ms": _milliseconds(started),
                "peak_locks": max(locks for _, locks in claims),
                "process_peak_rss_mib": _peak_rss_mib(),
                **_io_delta(before_io, after_io),
            }
        )
        before_io = after_io
    return results


def _intake(database_url: str) -> dict[str, object]:
    _reset_and_seed(database_url, 100)
    before_io = _database_io(database_url)
    runtime = PostgreSQLRuntime(
        PostgresDatabaseUrl(database_url),
        role=PostgreSQLRuntimeRole.ADMISSION,
    )
    runtime.open()
    config = admission.AdmissionConfig(
        database_url=PostgresDatabaseUrl(database_url),
        profile="default-standard",
        github_app_secret="benchmark-only",
        contract_environment={},
    )
    clients = 8
    start_barrier = Barrier(clients)
    metrics_lock = Lock()
    observed_waiters = 0

    def worker(indexes: range) -> list[int]:
        nonlocal observed_waiters
        rows: list[int] = []
        start_barrier.wait()
        for index in indexes:
            payload = {
                "action": "created",
                "installation": {"id": 1},
                "repository": {
                    "id": index + 1,
                    "full_name": f"benchmark/repo-{index + 1}",
                },
                "issue": {"number": 1, "pull_request": {"url": "benchmark"}},
                "comment": {
                    "id": index + 1,
                    "body": "/review",
                    "author_association": "MEMBER",
                },
                "sender": {
                    "id": index + 1,
                    "login": f"benchmark-{index + 1}",
                    "type": "User",
                },
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            response = admission.receive_github_app_delivery(
                body=body,
                payload=payload,
                delivery_id=str(UUID(int=index + 1)),
                event="issue_comment",
                config=config,
                runtime=runtime,
            )
            assert response.status == "received"
            rows.append(index + 1)
            with metrics_lock:
                observed_waiters = max(
                    observed_waiters, runtime.pool_metrics().waiting_requests
                )
        return rows

    started = time.perf_counter_ns()
    try:
        with ThreadPoolExecutor(max_workers=clients) as executor:
            batches = tuple(
                executor.map(
                    worker,
                    (range(offset, 100, clients) for offset in range(clients)),
                )
            )
        pool = runtime.pool_metrics()
    finally:
        runtime.close()
    identifiers = [item for batch in batches for item in batch]
    wall_ms = _milliseconds(started)
    return {
        "deliveries": len(set(identifiers)),
        "clients": clients,
        "wall_ms": wall_ms,
        "deliveries_per_second": round(len(identifiers) / (wall_ms / 1000), 1),
        "pool_waiters_peak": max(observed_waiters, pool.waiting_requests),
        "pool_maximum_size": pool.maximum_size,
        "process_peak_rss_mib": _peak_rss_mib(),
        **_io_delta(before_io, _database_io(database_url)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--reset-schema",
        action="store_true",
        required=True,
        help="Allow repeated schema resets in a database ending in _benchmark.",
    )
    args = parser.parse_args()
    database_name = conninfo_to_dict(args.database_url).get("dbname", "")
    if not database_name.endswith("_benchmark"):
        parser.error("--database-url must name a dedicated *_benchmark database")
    receipt = {
        "schema_version": 2,
        "candidate_revision": "v0.1.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "changed_files": _changed_files(),
        "queue": _queue(args.database_url),
        "repositories": _repositories(args.database_url),
        "publishers": _publishers(args.database_url),
        "app_intake": _intake(args.database_url),
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
